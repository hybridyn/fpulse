"""Approval notification service — sends notifications via ALL configured channels.

When a developer submits for review, approves, or rejects a pipeline, this
service:
  1. Creates in-app notifications for the target users
  2. Sends Email (SMTP), Slack, Teams, and/or Webhook notifications
     based on admin-configured notification settings

Channel configuration lives in admin_settings under the `notifications` key:
  {
    "channels": ["email", "slack", "teams", "webhook"],
    "smtp": { "host": "...", "port": 587, ... },
    "slack_webhook": "https://hooks.slack.com/...",
    "teams_webhook": "https://outlook.office.com/...",
    "webhook_url": "https://...",
    "notify_on": ["submit", "approve", "reject", "deploy"]
  }

All delivery is best-effort — a failed email never blocks the workflow action.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from fpulse.notifications.models import Notification

logger = logging.getLogger("fpulse.notifications")


class ApprovalNotifier:
    """Sends multi-channel notifications for the approval workflow.

    Looks up SMTP settings from admin_settings (DB) first, falls back to
    environment variables. This lets admins configure email from the UI
    without restarting the server.
    """

    def __init__(self, notification_store, user_store, db):
        self._store = notification_store
        self._user_store = user_store
        self._db = db

    # ── Public API ──────────────────────────────────────────────────────

    def on_submit_for_review(
        self,
        workflow_id: str,
        workflow_name: str,
        submitted_by_email: str,
        submitted_by_name: str,
    ):
        """Developer submitted a pipeline for PROD review.
        Notify all admins + super_admins.
        """
        config = self._get_config()
        if "submit" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return

        admins = self._get_admin_users()
        for admin in admins:
            notif = Notification(
                user_id=admin["id"],
                type="approval_request",
                title="Pipeline Submitted for Review",
                message=f"{submitted_by_name} ({submitted_by_email}) submitted \"{workflow_name}\" for PROD deployment.",
                link_type="approvals",
                link_id=workflow_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "submitted_by": submitted_by_email,
                },
            )
            self._store.create(notif)

        # Send external notifications
        subject = f"[F-Pulse] Pipeline Submitted: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Submitted by: {submitted_by_name} ({submitted_by_email})\n"
            f"Action Required: Review and approve/reject for PROD deployment\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        admin_emails = [a["email"] for a in admins if a.get("email")]
        self._send_all_channels(config, subject, body, admin_emails, "approval_request")

    def on_approved(
        self,
        workflow_id: str,
        workflow_name: str,
        approved_by_email: str,
        approved_by_name: str,
        submitted_by_user_id: str,
        notes: str = "",
    ):
        """Admin approved the pipeline. Notify the submitter."""
        config = self._get_config()
        if "approve" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return

        notif = Notification(
            user_id=submitted_by_user_id,
            type="approved",
            title="Pipeline Approved",
            message=f"Your pipeline \"{workflow_name}\" was approved by {approved_by_name}."
                    + (f" Notes: {notes}" if notes else ""),
            link_type="workflow",
            link_id=workflow_id,
            metadata={
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "approved_by": approved_by_email,
                "notes": notes,
            },
        )
        self._store.create(notif)

        # Email the submitter
        submitter = self._user_store.get_user(submitted_by_user_id)
        subject = f"[F-Pulse] Pipeline Approved: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Status: APPROVED\n"
            f"Approved by: {approved_by_name} ({approved_by_email})\n"
            + (f"Notes: {notes}\n" if notes else "")
            + f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"The pipeline is now ready for PROD deployment."
        )
        recipient_emails = [submitter.email] if submitter else []
        self._send_all_channels(config, subject, body, recipient_emails, "approved")

    def on_rejected(
        self,
        workflow_id: str,
        workflow_name: str,
        rejected_by_email: str,
        rejected_by_name: str,
        submitted_by_user_id: str,
        notes: str = "",
    ):
        """Admin rejected the pipeline. Notify the submitter."""
        config = self._get_config()
        if "reject" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return

        notif = Notification(
            user_id=submitted_by_user_id,
            type="rejected",
            title="Pipeline Rejected",
            message=f"Your pipeline \"{workflow_name}\" was rejected by {rejected_by_name}."
                    + (f" Reason: {notes}" if notes else ""),
            link_type="workflow",
            link_id=workflow_id,
            metadata={
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "rejected_by": rejected_by_email,
                "notes": notes,
            },
        )
        self._store.create(notif)

        submitter = self._user_store.get_user(submitted_by_user_id)
        subject = f"[F-Pulse] Pipeline Rejected: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Status: REJECTED\n"
            f"Rejected by: {rejected_by_name} ({rejected_by_email})\n"
            + (f"Reason: {notes}\n" if notes else "")
            + f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Please address the feedback and re-submit when ready."
        )
        recipient_emails = [submitter.email] if submitter else []
        self._send_all_channels(config, subject, body, recipient_emails, "rejected")

    def on_deployed(
        self,
        workflow_id: str,
        workflow_name: str,
        deployed_by_email: str,
        deployed_by_name: str,
        submitted_by_user_id: str = "",
    ):
        """Pipeline deployed to PROD. Notify the submitter + all admins."""
        config = self._get_config()
        if "deploy" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return

        # Notify submitter
        if submitted_by_user_id:
            notif = Notification(
                user_id=submitted_by_user_id,
                type="deployed",
                title="Pipeline Deployed to PROD",
                message=f"Your pipeline \"{workflow_name}\" has been deployed to production by {deployed_by_name}.",
                link_type="workflow",
                link_id=workflow_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "deployed_by": deployed_by_email,
                },
            )
            self._store.create(notif)

        subject = f"[F-Pulse] Pipeline Deployed: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Status: DEPLOYED TO PRODUCTION\n"
            f"Deployed by: {deployed_by_name} ({deployed_by_email})\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        admin_emails = [a["email"] for a in self._get_admin_users()]
        self._send_all_channels(config, subject, body, admin_emails, "deployed")

    # ── PR11/PR12 — new approval flows (Apr 26-27 2026) ────────────────

    def on_submitted_for_deploy(
        self,
        workflow_id: str,
        workflow_name: str,
        submitted_by: str,
        sandbox_run_id: str,
        gate1_approver: str,
    ):
        """Gate 2 — Prod admin submits an approved-and-sandboxed pipeline
        for final deploy approval. Notify all approvers with sandbox
        evidence so they can decide quickly."""
        config = self._get_config()
        if "submit" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return
        admins = self._get_admin_users()
        for admin in admins:
            self._store.create(Notification(
                user_id=admin["id"],
                type="approval_request",
                title="Gate 2: Deploy Approval Needed",
                message=(
                    f"\"{workflow_name}\" passed sandbox (run {sandbox_run_id[:12]}, "
                    f"approved at Gate 1 by {gate1_approver}). "
                    f"Submitted for final deploy approval by {submitted_by}."
                ),
                link_type="approvals",
                link_id=workflow_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "submitted_by": submitted_by,
                    "sandbox_run_id": sandbox_run_id,
                    "gate1_approver": gate1_approver,
                    "gate": "deploy",
                },
            ))
        subject = f"[F-Pulse] Deploy Approval Needed: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Stage: Gate 2 (Deploy Approval)\n"
            f"Sandbox evidence: {sandbox_run_id}\n"
            f"Gate 1 approved by: {gate1_approver}\n"
            f"Submitted for deploy by: {submitted_by}\n"
            f"Action Required: Review sandbox output and approve/reject deploy.\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_all_channels(
            config, subject, body,
            [a["email"] for a in admins if a.get("email")],
            "approval_request",
        )

    def on_lifecycle_requested(
        self,
        workflow_id: str,
        workflow_name: str,
        action: str,                       # 'activate' | 'deactivate'
        target_env: str,
        requested_by: str,
        reason: str = "",
    ):
        """User requested an Activate / Deactivate on a PROD pipeline.
        Notify all approvers — same channels as deploy approvals."""
        config = self._get_config()
        if "submit" not in config.get("notify_on", ["submit", "approve", "reject", "deploy"]):
            return
        admins = self._get_admin_users()
        action_word = action.capitalize()
        for admin in admins:
            self._store.create(Notification(
                user_id=admin["id"],
                type="lifecycle_request",
                title=f"{action_word} Request — {target_env.upper()}",
                message=(
                    f"{requested_by} requested to {action} \"{workflow_name}\" in {target_env.upper()}."
                    + (f" Reason: {reason}" if reason else "")
                ),
                link_type="approvals",
                link_id=workflow_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "action": action,
                    "target_env": target_env,
                    "requested_by": requested_by,
                    "reason": reason,
                },
            ))
        subject = f"[F-Pulse] {action_word} Request: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Action: {action_word} ({target_env.upper()})\n"
            f"Requested by: {requested_by}\n"
            f"Reason: {reason or '(none)'}\n"
            f"Action Required: Approve/reject in the Approvals page.\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send_all_channels(
            config, subject, body,
            [a["email"] for a in admins if a.get("email")],
            "lifecycle_request",
        )

    def on_lifecycle_decided(
        self,
        workflow_id: str,
        workflow_name: str,
        action: str,                       # 'activate' | 'deactivate'
        target_env: str,
        decided_by: str,
        decision: str,                     # 'approved' | 'rejected'
        decision_notes: str = "",
        requested_by_user_id: str | None = None,
    ):
        """Approver decided on a lifecycle request. Notify the original requester."""
        if not requested_by_user_id:
            return
        action_word = action.capitalize()
        outcome = "Approved" if decision == "approved" else "Rejected"
        self._store.create(Notification(
            user_id=requested_by_user_id,
            type="lifecycle_decided",
            title=f"{action_word} Request {outcome}",
            message=(
                f"Your request to {action} \"{workflow_name}\" in {target_env.upper()} "
                f"was {decision} by {decided_by}."
                + (f" Notes: {decision_notes}" if decision_notes else "")
            ),
            link_type="workflow",
            link_id=workflow_id,
            metadata={
                "workflow_id": workflow_id,
                "workflow_name": workflow_name,
                "action": action,
                "decision": decision,
                "decided_by": decided_by,
                "decision_notes": decision_notes,
            },
        ))

    # ── Execution alerts (May 3 2026) ──────────────────────────────────

    def on_long_running(
        self,
        workflow_id: str,
        workflow_name: str,
        execution_id: str,
        elapsed_minutes: int,
        threshold_minutes: int,
        triggered_by_user_id: str | None = None,
    ):
        """A pipeline has exceeded its long-running threshold."""
        config = self._get_config()
        if not config.get("notify_on_long_running", True):
            return

        recipients_user_ids: list[str] = []
        if triggered_by_user_id:
            recipients_user_ids.append(triggered_by_user_id)
        admins = self._get_admin_users()
        for admin in admins:
            if admin["id"] not in recipients_user_ids:
                recipients_user_ids.append(admin["id"])

        for user_id in recipients_user_ids:
            self._store.create(Notification(
                user_id=user_id,
                type="long_running",
                title=f"Long-running pipeline: {workflow_name}",
                message=(
                    f"\"{workflow_name}\" has been running for {elapsed_minutes} minutes "
                    f"(threshold: {threshold_minutes}m). Execution {execution_id[:12]}."
                ),
                link_type="executions",
                link_id=execution_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "execution_id": execution_id,
                    "elapsed_minutes": elapsed_minutes,
                    "threshold_minutes": threshold_minutes,
                },
            ))

        subject = f"[F-Pulse] Long-running pipeline: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Status: STILL RUNNING\n"
            f"Elapsed: {elapsed_minutes} minutes (threshold: {threshold_minutes}m)\n"
            f"Execution: {execution_id}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"Investigate the run via the Executions page or cancel if stuck."
        )
        self._send_all_channels(
            config, subject, body,
            [a["email"] for a in admins if a.get("email")],
            "long_running",
        )

    def on_schedule_miss(
        self,
        workflow_id: str,
        workflow_name: str,
        schedule_id: str,
        expected_run_at: datetime,
        miss_minutes: int,
    ):
        """A scheduled pipeline did not start within its expected window."""
        config = self._get_config()
        if not config.get("notify_on_schedule_miss", True):
            return

        admins = self._get_admin_users()
        for admin in admins:
            self._store.create(Notification(
                user_id=admin["id"],
                type="schedule_miss",
                title=f"Schedule miss: {workflow_name}",
                message=(
                    f"\"{workflow_name}\" was scheduled to run at "
                    f"{expected_run_at.strftime('%Y-%m-%d %H:%M UTC')} but did not start. "
                    f"Missed by {miss_minutes} minute(s)."
                ),
                link_type="schedules",
                link_id=schedule_id,
                metadata={
                    "workflow_id": workflow_id,
                    "workflow_name": workflow_name,
                    "schedule_id": schedule_id,
                    "expected_run_at": expected_run_at.isoformat(),
                    "miss_minutes": miss_minutes,
                },
            ))

        subject = f"[F-Pulse] Schedule miss: {workflow_name}"
        body = (
            f"Pipeline: {workflow_name}\n"
            f"Expected run: {expected_run_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Missed by: {miss_minutes} minute(s)\n"
            f"Schedule: {schedule_id}\n\n"
            f"Check scheduler health and pipeline status."
        )
        self._send_all_channels(
            config, subject, body,
            [a["email"] for a in admins if a.get("email")],
            "schedule_miss",
        )

    # ── Internal helpers ────────────────────────────────────────────────

    def _get_config(self) -> dict:
        """Read notification config from admin_settings, with env var fallbacks."""
        try:
            row = self._db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
            if row:
                settings = json.loads(row["data"])
                return settings.get("notifications", {})
        except Exception:
            pass
        return {}

    def _get_smtp_config(self, config: dict) -> dict:
        """Resolve SMTP settings: DB config wins, env vars as fallback."""
        smtp = config.get("smtp", {})
        return {
            "host": smtp.get("host") or os.environ.get("SMTP_HOST", ""),
            "port": int(smtp.get("port") or os.environ.get("SMTP_PORT", "587")),
            "user": smtp.get("user") or os.environ.get("SMTP_USER", ""),
            "password": smtp.get("password") or os.environ.get("SMTP_PASS", ""),
            "from_email": smtp.get("from_email") or os.environ.get("SMTP_FROM", "fpulse@localhost"),
            "tls": smtp.get("tls", True) if "tls" in smtp else os.environ.get("SMTP_TLS", "true").lower() == "true",
        }

    def _get_admin_users(self) -> list[dict]:
        """Get all active admins and super_admins."""
        users = self._user_store.list_users()
        return [u for u in users if u.get("role") in ("admin", "super_admin") and u.get("is_active", True)]

    def _send_all_channels(
        self,
        config: dict,
        subject: str,
        body: str,
        email_recipients: list[str],
        event_type: str,
    ):
        """Send to all configured channels. Best-effort — never raises.

        Body is LLM-summarized once before fan-out (Step 3 of the AI arc) so
        every channel sees the same enhanced text. On any LLM failure the
        original static body is used unchanged.
        """
        channels = config.get("channels", ["email"])

        try:
            from fpulse.ai.notification_summary import summarize_notification_body
            body, _ai_powered = summarize_notification_body(
                event_type=event_type,
                subject=subject,
                body=body,
            )
        except Exception:
            pass

        # Email
        if "email" in channels and email_recipients:
            try:
                self._send_email(config, subject, body, email_recipients)
            except Exception as exc:
                logger.warning("Notification email failed (non-fatal): %s", exc)

        # Slack
        if "slack" in channels:
            webhook = config.get("slack_webhook", "")
            if webhook:
                try:
                    self._send_slack(webhook, subject, body)
                except Exception as exc:
                    logger.warning("Slack notification failed (non-fatal): %s", exc)

        # Teams
        if "teams" in channels:
            webhook = config.get("teams_webhook", "")
            if webhook:
                try:
                    self._send_teams(webhook, subject, body)
                except Exception as exc:
                    logger.warning("Teams notification failed (non-fatal): %s", exc)

        # Discord
        if "discord" in channels:
            webhook = config.get("discord_webhook", "")
            if webhook:
                try:
                    self._send_discord(webhook, subject, body)
                except Exception as exc:
                    logger.warning("Discord notification failed (non-fatal): %s", exc)

        # Generic Webhook
        if "webhook" in channels:
            url = config.get("webhook_url", "")
            if url:
                try:
                    self._send_webhook(url, subject, body, event_type)
                except Exception as exc:
                    logger.warning("Webhook notification failed (non-fatal): %s", exc)

    # ── Channel implementations ─────────────────────────────────────────

    def _send_email(self, config: dict, subject: str, body: str, recipients: list[str]):
        """Send via SMTP."""
        smtp = self._get_smtp_config(config)
        if not smtp["host"]:
            logger.info("[EMAIL-DRY-RUN] To: %s | Subject: %s | Body: %s", ", ".join(recipients), subject, body[:200])
            return

        msg = MIMEMultipart("alternative")
        msg["From"] = smtp["from_email"]
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(self._html_email(subject, body), "html"))

        if smtp["tls"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=10) as server:
                server.starttls(context=ctx)
                if smtp["user"]:
                    server.login(smtp["user"], smtp["password"])
                server.sendmail(smtp["from_email"], recipients, msg.as_string())
        else:
            with smtplib.SMTP(smtp["host"], smtp["port"], timeout=10) as server:
                if smtp["user"]:
                    server.login(smtp["user"], smtp["password"])
                server.sendmail(smtp["from_email"], recipients, msg.as_string())

        logger.info("Email sent to %s: %s", ", ".join(recipients), subject)

    def _send_slack(self, webhook_url: str, subject: str, body: str):
        """Send Slack notification via webhook."""
        payload = json.dumps({
            "text": f"*{subject}*\n```{body}```",
        }).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Slack notification sent: %s", subject)

    def _send_teams(self, webhook_url: str, subject: str, body: str):
        """Send Teams notification via webhook (Adaptive Card)."""
        card = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": subject, "weight": "Bolder", "size": "Medium"},
                        {"type": "TextBlock", "text": body, "wrap": True},
                    ],
                },
            }],
        }
        payload = json.dumps(card).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Teams notification sent: %s", subject)

    def _send_discord(self, webhook_url: str, subject: str, body: str):
        """Send Discord notification via webhook."""
        payload = json.dumps({
            "embeds": [{
                "title": subject,
                "description": body[:4096],
                "color": 0xF5A623,
            }],
        }).encode()
        req = urllib.request.Request(
            webhook_url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Discord notification sent: %s", subject)

    def _send_webhook(self, url: str, subject: str, body: str, event_type: str):
        """Send generic webhook notification."""
        payload = json.dumps({
            "event": event_type,
            "subject": subject,
            "body": body,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "fpulse",
        }).encode()
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
        logger.info("Webhook notification sent to %s: %s", url, subject)

    def _html_email(self, subject: str, body: str) -> str:
        lines_html = body.replace("\n", "<br>")
        return f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="background: linear-gradient(135deg, #F5A623, #D4880A); padding: 16px 24px; border-radius: 12px 12px 0 0;">
                <h2 style="color: white; margin: 0; font-size: 16px;">⚡ F-Pulse</h2>
                <p style="color: rgba(255,255,255,0.85); margin: 4px 0 0; font-size: 13px;">{subject}</p>
            </div>
            <div style="border: 1px solid #e2e8f0; border-top: none; border-radius: 0 0 12px 12px; padding: 24px; background: #fafafa;">
                <p style="font-size: 14px; color: #334155; line-height: 1.6;">{lines_html}</p>
            </div>
            <p style="font-size: 11px; color: #94a3b8; margin-top: 12px; text-align: center;">
                Sent by F-Pulse Pipeline Orchestrator
            </p>
        </body>
        </html>
        """
