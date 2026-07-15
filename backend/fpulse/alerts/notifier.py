"""
Real notification delivery — sends alerts via Email, Slack, Teams, and Webhook.

Uses only Python standard library (smtplib, urllib, json, ssl).
Falls back gracefully if SMTP is not configured.
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

from fpulse.alerts.models import AlertRule, AlertChannel, AlertLog

logger = logging.getLogger("fpulse.notifier")


class NotificationService:
    """Sends real notifications via configured channels."""

    def __init__(self):
        # SMTP config — DB wins, env vars are the fallback. Re-read on
        # every NotificationService instance so the user can edit
        # SMTP via the Settings UI and have it take effect on the
        # next alert without a backend restart.
        cfg = self._load_smtp_config()
        self.smtp_host = cfg["host"]
        self.smtp_port = cfg["port"]
        self.smtp_user = cfg["user"]
        self.smtp_pass = cfg["password"]
        self.smtp_from = cfg["from_email"]
        self.smtp_tls = cfg["tls"]

    @staticmethod
    def _load_smtp_config() -> dict:
        """Resolve SMTP settings: DB config wins, env vars as fallback.

        Mirrors the same pattern as
        ``fpulse.notifications.service.NotificationService._get_smtp_config``
        so a single Settings → Notifications → SMTP form drives both
        notification systems. Without this, alerts saw env-vars-only
        and the Settings UI couldn't fix delivery without a server
        restart — exactly the dead-end the user hit on 2026-05-09.
        """
        db_cfg: dict = {}
        try:
            from fpulse.main import app_state
            db = app_state.get("db") if isinstance(app_state, dict) else None
            if db is not None:
                row = db.fetchone("SELECT data FROM settings WHERE id = 'admin_settings'")
                if row:
                    import json as _json
                    settings = _json.loads(row["data"])
                    db_cfg = (settings.get("notifications") or {}).get("smtp") or {}
        except Exception:
            db_cfg = {}

        def _get(key: str, env_key: str, default: str = "") -> str:
            v = db_cfg.get(key)
            return v if v not in (None, "") else os.environ.get(env_key, default)

        port_raw = db_cfg.get("port") or os.environ.get("SMTP_PORT", "587")
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            port = 587

        # TLS: explicit DB value > env var > default true
        if "tls" in db_cfg:
            tls = bool(db_cfg["tls"])
        else:
            tls = os.environ.get("SMTP_TLS", "true").lower() == "true"

        return {
            "host": _get("host", "SMTP_HOST", ""),
            "port": port,
            "user": _get("user", "SMTP_USER", ""),
            "password": _get("password", "SMTP_PASS", ""),
            "from_email": _get("from_email", "SMTP_FROM", "fpulse@localhost"),
            "tls": tls,
        }

    def send(
        self,
        rule: AlertRule,
        context: dict[str, Any],
    ) -> AlertLog:
        """
        Send a notification based on the rule's channel.

        context should contain:
          - workflow_name: str
          - execution_id: str (optional)
          - status: str (success/error/running)
          - duration_ms: int (optional)
          - error_message: str (optional)
          - triggered_condition: str
        """
        workflow_name = context.get("workflow_name", "Unknown Pipeline")
        status = context.get("status", "unknown")
        execution_id = context.get("execution_id", "")
        condition = context.get("triggered_condition", rule.condition.value)

        # Build message
        subject = f"[F-Pulse OSS] {workflow_name} — {status.upper()}"
        body = self._build_message(rule, context)

        # Dispatch based on channel
        try:
            if rule.channel == AlertChannel.EMAIL:
                self._send_email(rule, subject, body, context)
            elif rule.channel == AlertChannel.SLACK:
                self._send_slack(rule, workflow_name, status, body)
            elif rule.channel == AlertChannel.TEAMS:
                self._send_teams(rule, workflow_name, status, body)
            elif rule.channel == AlertChannel.WEBHOOK:
                self._send_webhook(rule, context)
            else:
                logger.warning(f"Unsupported channel: {rule.channel}")
                return AlertLog(
                    rule_id=rule.id,
                    workflow_id=rule.workflow_id or "",
                    execution_id=execution_id,
                    channel=rule.channel,
                    condition=rule.condition,
                    status="failed",
                    message=body,
                    error=f"Unsupported channel: {rule.channel}",
                )

            logger.info(f"Alert sent via {rule.channel.value} for '{workflow_name}' ({condition})")
            return AlertLog(
                rule_id=rule.id,
                workflow_id=rule.workflow_id or "",
                execution_id=execution_id,
                channel=rule.channel,
                condition=rule.condition,
                status="sent",
                message=body,
            )

        except Exception as e:
            logger.error(f"Failed to send {rule.channel.value} alert: {e}")
            return AlertLog(
                rule_id=rule.id,
                workflow_id=rule.workflow_id or "",
                execution_id=execution_id,
                channel=rule.channel,
                condition=rule.condition,
                status="failed",
                message=body,
                error=str(e),
            )

    def send_simple_email(self, to: str, subject: str, body: str) -> None:
        """Send a plain-text email outside the AlertRule workflow.

        Reused by non-alert callers (currently the forgot-password flow)
        that need to deliver a one-shot message via the same SMTP path
        the alerts pipeline uses. Re-reads the DB-backed config on every
        call so a SMTP edit in Admin -> Settings takes effect on the
        next send without a restart.

        Raises RuntimeError when SMTP isn't configured; the caller is
        expected to gate on ``self.smtp_host`` first and pick its own
        no-SMTP fallback (the forgot-password flow falls back to
        returning the token inline).
        """
        cfg = self._load_smtp_config()
        self.smtp_host = cfg["host"]
        self.smtp_port = cfg["port"]
        self.smtp_user = cfg["user"]
        self.smtp_pass = cfg["password"]
        self.smtp_from = cfg["from_email"]
        self.smtp_tls = cfg["tls"]

        if not self.smtp_host:
            raise RuntimeError(
                "SMTP is not configured. Configure it in Admin -> Settings "
                "or set SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS env vars."
            )

        # Use quoted-printable so URLs and ASCII text appear verbatim in
        # the wire payload — the default utf-8 charset for MIMEText falls
        # back to base64, which renders fine in clients but makes the
        # body unreadable in raw logs / debug captures.
        from email.charset import Charset, QP
        cs = Charset("utf-8")
        cs.body_encoding = QP
        msg = MIMEText("", "plain")
        msg.set_charset(cs)
        msg.set_payload(body, cs)
        msg["From"] = self.smtp_from
        msg["To"] = to
        msg["Subject"] = subject

        if self.smtp_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls(context=ctx)
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_from, [to], msg.as_string())
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_from, [to], msg.as_string())

    def _build_message(self, rule: AlertRule, context: dict) -> str:
        """Build notification message body.

        For failure alerts the body is laid out in three labelled
        sections so the recipient can scan them quickly:

          PIPELINE
          ────────
          Pipeline / Status / Condition / Duration / Time

          ORIGINAL FAILURE
          ────────────────
          Error message + first failed step (when known)

          AI DIAGNOSIS
          ────────────
          Plain-English diagnosis + suggested fix (rule-based, fast).
          Only included when the diagnoser matched a known pattern.

        Custom-message templates still take precedence — power users can
        opt out of the structured layout entirely. ``deep_link`` and
        every payload key remain available as ``{{var}}`` substitutions.
        """
        if rule.custom_message:
            msg = rule.custom_message
            for key, value in context.items():
                msg = msg.replace(f"{{{{{key}}}}}", str(value))
            return msg

        workflow = context.get("workflow_name", "Unknown")
        status = context.get("status", "unknown")
        condition = context.get("triggered_condition", rule.condition.value)
        duration = context.get("duration_ms")
        error = context.get("error_message")
        first_failed_step = context.get("first_failed_step") or ""
        ai_diagnosis = context.get("ai_diagnosis") or ""
        ai_suggestion = context.get("ai_suggestion") or ""
        deep_link = context.get("deep_link") or ""

        sections: list[str] = []

        # ── Pipeline section ──
        pipeline_lines = [
            "PIPELINE",
            "--------",
            f"Pipeline: {workflow}",
            f"Status:   {status.upper()}",
            f"Condition: {condition.replace('_', ' ').title()}",
        ]
        if duration:
            pipeline_lines.append(f"Duration: {duration / 1000:.1f}s")
        pipeline_lines.append(
            f"Time:     {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        sections.append("\n".join(pipeline_lines))

        # ── Original Failure section (only if there's an error) ──
        if error:
            failure_lines = ["ORIGINAL FAILURE", "----------------"]
            if first_failed_step:
                failure_lines.append(f"Failed step: {first_failed_step}")
            failure_lines.append(error)
            sections.append("\n".join(failure_lines))

        # ── AI Diagnosis section (only when the diagnoser matched) ──
        if ai_diagnosis or ai_suggestion:
            ai_lines = ["AI DIAGNOSIS", "------------"]
            if ai_diagnosis:
                ai_lines.append(f"Diagnosis: {ai_diagnosis}")
            if ai_suggestion:
                ai_lines.append(f"Suggestion: {ai_suggestion}")
            sections.append("\n".join(ai_lines))

        # ── Open in F-Pulse link ──
        if deep_link:
            sections.append(f"Open in F-Pulse:\n{deep_link}")

        return "\n\n".join(sections)

    # ── Email ──

    def _send_email(self, rule: AlertRule, subject: str, body: str, context: dict | None = None) -> None:
        """Send email via SMTP."""
        recipients = rule.email_addresses
        if not recipients:
            raise ValueError("No email addresses configured")

        # Debug: log what this instance actually saw at __init__ time.
        # Helps diagnose "I configured SMTP via UI but emails still
        # don't go out" — usually means the instance was constructed
        # before the DB write completed, or the DB read silently
        # returned an empty config.
        logger.debug(
            "EMAIL DISPATCH instance smtp_host=%r port=%r user=%r tls=%r",
            self.smtp_host, self.smtp_port, self.smtp_user, self.smtp_tls,
        )
        # Re-read SMTP from DB right before sending so a config saved
        # AFTER this NotificationService instance was constructed still
        # takes effect on this dispatch. Without this re-read, an
        # instance cached at request-handler entry uses whatever was
        # in the DB at construction time — fine for /health/ready
        # which constructs a fresh instance every check, but a real
        # gotcha for the test endpoint and any future caller that
        # caches instances.
        try:
            cfg = self._load_smtp_config()
            self.smtp_host = cfg["host"]
            self.smtp_port = cfg["port"]
            self.smtp_user = cfg["user"]
            self.smtp_pass = cfg["password"]
            self.smtp_from = cfg["from_email"]
            self.smtp_tls = cfg["tls"]
            logger.debug("EMAIL DISPATCH after live re-read smtp_host=%r", self.smtp_host)
        except Exception as exc:
            logger.warning("SMTP live re-read failed (using __init__ snapshot): %s", exc)

        if not self.smtp_host:
            # No SMTP configured — log the body for dev visibility AND
            # raise so the alert log records status="failed" with a
            # clear reason. Returning silently used to mark the alert
            # "sent" while nothing reached the recipient — exactly the
            # silent failure the user hit when configuring a Gmail
            # alert without setting SMTP_HOST/USER/PASS first.
            logger.info(f"[EMAIL-DRY-RUN] To: {', '.join(recipients)} | Subject: {subject}")
            logger.info(f"[EMAIL-DRY-RUN] Body: {body}")
            raise RuntimeError(
                "SMTP is not configured on this server. Set SMTP_HOST, "
                "SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM env vars "
                "and restart, or use Slack/Teams/Webhook for alerts."
            )

        msg = MIMEMultipart("alternative")
        msg["From"] = self.smtp_from
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject

        # Plain text body
        msg.attach(MIMEText(body, "plain"))

        # HTML version — context-rich layout (Pipeline table + lineage +
        # structured Original Failure / AI Diagnosis cards). Falls back
        # to a body-wrapped HTML when context is missing so legacy
        # callers still get something reasonable.
        html_body = self._build_html_email(subject, body, context or {})
        msg.attach(MIMEText(html_body, "html"))

        if self.smtp_tls:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                server.starttls(context=ctx)
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_from, recipients, msg.as_string())
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.smtp_from, recipients, msg.as_string())

    def _build_html_email(self, subject: str, body: str, context: dict | None = None) -> str:
        """Render the alert email as a structured HTML message.

        Layout (top → bottom). Redesigned 2026-05-21 to drop the redundant
        "F-Pulse OSS Alert" banner + the redundant 5-column pipeline
        table — both repeated info the email subject already carries
        verbatim and wasted ~30% of the vertical real estate.

          1. Compact header — pipeline name (once) + status chip (once)
             on a single line, with a thin condition / duration / time
             meta-line below.
          2. Run Details — Project name, Folder name, Trigger, Started,
             Steps, Rows, Owner. Always rendered when ANY field is
             present (was previously gated on ≥2 — too aggressive).
          3. LINEAGE — horizontal node flow with the failed step
             highlighted (rendered only when ``workflow_steps`` is in
             context).
          4. ORIGINAL FAILURE — failed-step badge + monospace error block
          5. AI DIAGNOSIS — diagnosis + suggestion in an indigo card,
             mirroring the in-app card on the Execution Summary page
          6. Footer — "Sent by F-Pulse OSS"

        ``context`` keys (all optional, missing keys degrade gracefully):
          workflow_name, status, duration_ms, triggered_condition,
          error_message, first_failed_step, ai_diagnosis, ai_suggestion,
          workflow_steps, workflow_connections, triggered_by,
          schedule_name, started_at, completed_at, workspace_id,
          project_id, folder_id, environment, rows_processed,
          steps_completed, steps_total, owner_email, deep_link,
          workflow_link, execution_link.

        Lineage rendering switches mode based on ``workflow_connections``:
          • absent / empty → flat horizontal `A → B → C` (the
            pre-2026-05-28 layout — kept for backwards compat with the
            handful of callers that pass steps but no edges).
          • non-empty → layered DAG: steps grouped into rank-columns
            (rank = longest path from any source), within a column
            stacked vertically. Parallel branches and joins are
            visible at a glance — matching the in-app Execution
            Summary view.
        """
        from html import escape as _esc

        ctx = context or {}
        workflow = _esc(str(ctx.get("workflow_name", "Unknown")))
        status = str(ctx.get("status", "unknown")).lower()
        condition = str(ctx.get("triggered_condition", "")).replace("_", " ").title() or "—"
        duration_ms = ctx.get("duration_ms")
        duration_str = f"{duration_ms / 1000:.1f}s" if duration_ms else "—"
        time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        error_msg = ctx.get("error_message") or ""
        failed_step = ctx.get("first_failed_step") or ""
        ai_diagnosis = ctx.get("ai_diagnosis") or ""
        ai_suggestion = ctx.get("ai_suggestion") or ""
        ai_severity = (ctx.get("ai_severity") or "").lower()
        ai_powered = bool(ctx.get("ai_powered"))
        workflow_steps = ctx.get("workflow_steps") or []
        # Edges between steps — list of {"from": step_id, "to": step_id}
        # (legacy callers used StepConnection field names so accept
        # `from_step` / `to_step` too). When this is non-empty we
        # render a layered DAG instead of a flat row of cards. See
        # the lineage block below for the layout algorithm.
        workflow_connections = ctx.get("workflow_connections") or []

        # ── Complex-pipeline context (all optional) ──────────────
        # Populated when the executor has richer state to share. Each
        # section is conditional so simple linear pipelines render
        # exactly the same way they did before.
        step_path = ctx.get("step_path") or []
        iteration = ctx.get("failed_step_iteration") or {}
        attempts = ctx.get("failed_step_attempts") or 0
        input_snippet = ctx.get("failed_step_input_snippet") or ""
        step_metrics = ctx.get("step_metrics") or []
        resume_available = bool(ctx.get("resume_available"))
        resume_token = ctx.get("resume_token") or ""
        loop_progress = ctx.get("loop_progress_summary") or {}

        # ── Run-details metadata (for the Run Details section) ───
        execution_id = ctx.get("execution_id") or ""
        workflow_id = ctx.get("workflow_id") or ""
        schedule_name = ctx.get("schedule_name") or ""
        triggered_by = ctx.get("triggered_by") or ""
        started_at = ctx.get("started_at") or ""
        completed_at = ctx.get("completed_at") or ""
        workspace_id = ctx.get("workspace_id") or ""
        project_id = ctx.get("project_id") or ""
        folder_id = ctx.get("folder_id") or ""
        environment = ctx.get("environment") or ""
        # 2026-05-21: extra signals the operator asked for — what triggered
        # the run, what got processed, who owns it.
        rows_processed = ctx.get("rows_processed")
        steps_completed = ctx.get("steps_completed")
        steps_total = ctx.get("steps_total")
        owner_email = ctx.get("owner_email") or ""
        deep_link = ctx.get("deep_link") or ""
        workflow_link = ctx.get("workflow_link") or ""
        execution_link = ctx.get("execution_link") or deep_link

        # Resolve project_id + folder_id to human names via app_state so the
        # email reads "Company Testing / siva" instead of two opaque UUIDs.
        # Stores might be unavailable in tests; fall back to the raw id.
        project_name = project_id
        folder_name = ""
        if project_id or folder_id:
            try:
                from fpulse.main import app_state as _as
                if project_id:
                    pstore = _as.get("project_store")
                    if pstore is not None:
                        try:
                            proj = pstore.get(project_id)
                            if proj and getattr(proj, "name", None):
                                project_name = proj.name
                        except Exception:
                            pass
                if folder_id:
                    fstore = _as.get("folder_store")
                    if fstore is not None:
                        try:
                            fld = fstore.get(folder_id)
                            if fld and getattr(fld, "name", None):
                                folder_name = fld.name
                        except Exception:
                            pass
            except Exception:
                pass

        # Status pill colours mirror the in-app status chips so an alert
        # email feels like the same product, not a separate channel.
        pill_bg, pill_fg = {
            "success": ("#d1fae5", "#065f46"),
            "error":   ("#fee2e2", "#991b1b"),
            "failed":  ("#fee2e2", "#991b1b"),
            "running": ("#dbeafe", "#1e40af"),
        }.get(status, ("#e2e8f0", "#475569"))

        # ── HEADER — compact single-line pipeline + status ────────
        # 2026-05-21: replaces both the orange "F-Pulse OSS Alert" banner
        # AND the old 5-column pipeline table. The email subject already
        # says `[F-Pulse OSS] {workflow} — {STATUS}` so repeating that
        # info inside the body was pure decoration. Now the pipeline name
        # appears once (bold) with the status chip on the right; the
        # meta-line carries Condition · Duration · Time on one row.
        status_pill_html = (
            f"<span style='display:inline-block;padding:5px 14px;border-radius:9999px;"
            f"background:{pill_bg};color:{pill_fg};font-size:11px;font-weight:700;"
            f"letter-spacing:.06em;text-transform:uppercase'>{_esc(status)}</span>"
        )

        meta_bits: list[str] = []
        if condition and condition != "—":
            meta_bits.append(_esc(condition))
        meta_bits.append(_esc(duration_str))
        meta_bits.append(_esc(time_str))
        meta_line = (
            "<span style='color:#94a3b8;margin:0 8px'>·</span>".join(
                f"<span>{m}</span>" for m in meta_bits
            )
        )

        pipeline_table = (
            "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%;"
            "background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;"
            "box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
            "<tr>"
            "<td style='padding:18px 22px;vertical-align:middle'>"
            f"<div style='font-size:17px;font-weight:700;color:#0f172a;"
            f"word-break:break-word;line-height:1.25'>{_esc(workflow)}</div>"
            f"<div style='margin-top:6px;font-size:12px;color:#64748b;"
            f"font-family:ui-monospace,Consolas,Monaco,monospace'>{meta_line}</div>"
            "</td>"
            f"<td style='padding:18px 22px;text-align:right;vertical-align:middle;white-space:nowrap'>"
            f"{status_pill_html}</td>"
            "</tr>"
            "</table>"
        )

        # ── RUN DETAILS — operational metadata about THIS run ────
        # Lets the recipient identify *which* execution this is (out
        # of potentially hundreds per day) without having to log in
        # and grep workspaces. Renders as a compact 2-column table of
        # whichever fields are populated; fields the scheduler didn't
        # supply are simply skipped.
        def _short_ts(iso: str) -> str:
            """Render an ISO timestamp as 'YYYY-MM-DD HH:MM:SS UTC'.
            Tolerates trailing-Z and missing tz — never raises."""
            if not iso:
                return ""
            try:
                s = iso.replace("Z", "+00:00") if iso.endswith("Z") else iso
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except (TypeError, ValueError):
                return iso

        # Trigger context — combines the source ('schedule' / 'manual' /
        # 'api') with the schedule name when present so the value is
        # readable end-to-end ('Schedule "daily-sync"').
        if triggered_by == "schedule" and schedule_name:
            trigger_str = f'Schedule "{schedule_name}"'
        elif triggered_by:
            trigger_str = triggered_by.replace("_", " ").title()
        else:
            trigger_str = ""

        # 2026-05-21: redesigned row list — operational signals on top,
        # noisy hex IDs (Execution ID / Pipeline ID) dropped because the
        # "View Execution" button below already covers navigation. Order
        # is what an operator reads top-to-bottom when triaging.
        run_detail_rows: list[tuple[str, str, bool]] = []  # (label, value, is_mono)
        if project_name:
            run_detail_rows.append(("Project", project_name, False))
        if folder_name:
            run_detail_rows.append(("Folder", folder_name, False))
        if trigger_str:
            run_detail_rows.append(("Trigger", trigger_str, False))
        if started_at:
            run_detail_rows.append(("Started", _short_ts(started_at), True))
        # Steps + Rows are signal-rich enough that we render them even when
        # they're zero (zero rows on a success run is itself worth seeing).
        if steps_completed is not None and steps_total is not None:
            run_detail_rows.append((
                "Steps", f"{int(steps_completed)} / {int(steps_total)}", False,
            ))
        if isinstance(rows_processed, (int, float)) and rows_processed >= 0:
            try:
                rows_str = f"{int(rows_processed):,}"
            except (TypeError, ValueError):
                rows_str = str(rows_processed)
            run_detail_rows.append(("Rows", rows_str, False))
        if owner_email:
            run_detail_rows.append(("Owner", owner_email, False))
        if environment:
            run_detail_rows.append(("Environment", environment.upper(), False))

        # Render Run Details whenever ANY operational field is present.
        # Previous gate (≥2 rows) skipped the section for simple-pipeline
        # emails — exactly the case the operator complained about.
        #
        # 2026-05-21 (later): switched from row-wise (LABEL | value per
        # row) to COLUMN-WISE — single header row with all labels, single
        # data row with all values — same shape as the original 5-column
        # Pipeline table. Operator preference: horizontal scanning beats
        # vertical scrolling for tabular metadata at this density (≤7
        # fields).
        run_details_html = ""
        if len(run_detail_rows) >= 1:
            th_style = (
                "padding:10px 14px;text-align:left;font-size:10px;font-weight:700;"
                "color:#475569;text-transform:uppercase;letter-spacing:.06em;"
                "background:#f1f5f9;border:1px solid #e2e8f0;vertical-align:middle;"
                "white-space:nowrap"
            )
            td_style = (
                "padding:12px 14px;font-size:13px;color:#0f172a;"
                "border:1px solid #e2e8f0;background:#ffffff;vertical-align:top;"
                "word-break:break-word"
            )
            mono_style = (
                ";font-family:ui-monospace,Consolas,Monaco,monospace;font-size:12px"
            )
            th_cells: list[str] = []
            td_cells: list[str] = []
            for label, value, is_mono in run_detail_rows:
                v_style = td_style + (mono_style if is_mono else "")
                th_cells.append(f"<th style='{th_style}'>{_esc(label)}</th>")
                td_cells.append(f"<td style='{v_style}'>{_esc(value)}</td>")
            run_details_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Run Details</div>"
                "<div style='overflow-x:auto;border-radius:10px;"
                "box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;"
                "width:100%;border-radius:10px;overflow:hidden'>"
                "<tr>" + "".join(th_cells) + "</tr>"
                "<tr>" + "".join(td_cells) + "</tr>"
                "</table></div></div>"
            )

        # 2026-05-21: Action button row (View Execution / Open Pipeline
        # / View All Runs) removed at operator request — the email is for
        # at-a-glance status, not in-app navigation. The body now ends on
        # Lineage (or whatever the last conditional section is) and the
        # footer signature. Deep-link fields stay on the payload so other
        # channels (Slack/Teams/webhook) can still surface them.

        # ── LINEAGE — DAG layout that matches the in-app Execution
        # Summary lineage view. Each step renders as a card with:
        #   • coloured left bar + tinted card background by status
        #   • bold step name + monospace step type
        #   • status badge pill in the top-right
        #   • metrics row at the bottom — ◈ rows · ⏱ duration — same as UI
        #   • thick red outline on the failed step
        #
        # Layout switches on whether ``workflow_connections`` is
        # supplied:
        #
        #   • empty/missing → flat horizontal `A → B → C` row of
        #     cards. The pre-2026-05-28 behaviour. Kept for legacy
        #     callers that supply only a flat step list.
        #
        #   • non-empty → layered DAG. Each step gets a rank (longest
        #     path from any source); steps with the same rank stack
        #     in one column; columns flow left-to-right with `→`
        #     separators. Parallel branches and joins (e.g. two
        #     sources feeding one pivot) finally render as parallel
        #     branches and joins instead of a misleading linear
        #     chain. Fixes the user complaint that the email
        #     misrepresents pipelines like Sales Pivot + Trend
        #     Analysis (2 sources, multiple joins) as `A → B → C → D`.
        #
        # Email-client constraint: stick to CSS that survives
        # Outlook / Gmail rendering — nested tables only, no flexbox,
        # no grid, no SVG (Outlook desktop strips inline <svg>).
        lineage_html = ""
        if isinstance(workflow_steps, list) and workflow_steps:
            def _fmt_rows(n) -> str:
                try:
                    n = int(n or 0)
                except (TypeError, ValueError):
                    return ""
                if n <= 0:
                    return ""
                return f"{n:,} rows"

            def _fmt_dur(ms) -> str:
                try:
                    ms = float(ms or 0)
                except (TypeError, ValueError):
                    return ""
                if ms <= 0:
                    return ""
                if ms < 1000:
                    return f"{int(ms)}ms"
                return f"{ms / 1000:.1f}s"

            def _render_step_card(s, idx: int) -> str:
                """Render one step card. Pure function — no layout
                dependencies, just produces the table cell wrapping
                the card. Layout caller decides where to put it.
                """
                if isinstance(s, dict):
                    s_name = str(s.get("name") or s.get("id") or f"step{idx+1}")
                    s_type = str(s.get("type") or "")
                    s_status = str(s.get("status") or "").lower()
                    s_rows = s.get("rows_processed")
                    s_dur = s.get("duration_ms")
                else:
                    s_name = str(s)
                    s_type = ""
                    s_status = ""
                    s_rows = None
                    s_dur = None

                # Status palette — mirrors the in-app LINEAGE_STATUS_STYLES.
                if s_status == "success":
                    bg, fg, border, bar = "#ecfdf5", "#065f46", "#a7f3d0", "#10b981"
                    badge_bg, badge_fg = "#d1fae5", "#065f46"
                elif s_status in ("error", "failed"):
                    bg, fg, border, bar = "#fef2f2", "#991b1b", "#fca5a5", "#ef4444"
                    badge_bg, badge_fg = "#fee2e2", "#991b1b"
                elif s_status == "running":
                    bg, fg, border, bar = "#eff6ff", "#1e40af", "#bfdbfe", "#f59e0b"
                    badge_bg, badge_fg = "#dbeafe", "#1e40af"
                elif s_status == "skipped":
                    bg, fg, border, bar = "#fffbeb", "#92400e", "#fde68a", "#92400e"
                    badge_bg, badge_fg = "#fef3c7", "#92400e"
                else:
                    bg, fg, border, bar = "#f8fafc", "#475569", "#e2e8f0", "#94a3b8"
                    badge_bg, badge_fg = "#e2e8f0", "#475569"
                outline = (
                    "box-shadow:0 0 0 2px #ef4444;"
                    if s_status in ("error", "failed") else ""
                )

                # Status pill (top-right of card).
                status_label = (s_status or "pending").upper()
                status_badge = (
                    f"<span style='display:inline-block;padding:2px 8px;border-radius:4px;"
                    f"background:{badge_bg};color:{badge_fg};font-size:9px;font-weight:800;"
                    f"letter-spacing:.06em;font-family:system-ui'>{_esc(status_label)}</span>"
                )

                type_html = (
                    f"<div style='font-size:10px;color:{fg};opacity:.7;font-family:ui-monospace,Consolas,Monaco,monospace;margin-top:3px'>{_esc(s_type)}</div>"
                    if s_type else ""
                )

                # Metrics row — rows + duration, only renders when present.
                rows_str = _fmt_rows(s_rows)
                dur_str = _fmt_dur(s_dur)
                metric_bits: list[str] = []
                if rows_str:
                    metric_bits.append(
                        f"<span style='color:#475569;font-weight:600'>◈ {_esc(rows_str)}</span>"
                    )
                if dur_str:
                    metric_bits.append(
                        f"<span style='color:#64748b;font-family:ui-monospace,Consolas,Monaco,monospace'>⏱ {_esc(dur_str)}</span>"
                    )
                metrics_html = ""
                if metric_bits:
                    sep = "<span style='color:#cbd5e1;margin:0 6px'>·</span>"
                    metrics_html = (
                        f"<div style='margin-top:10px;padding-top:8px;border-top:1px solid {border};"
                        f"font-size:11px;text-align:left'>"
                        f"{sep.join(metric_bits)}</div>"
                    )

                # The card itself: inner table so left status-bar + body
                # render side-by-side reliably in Outlook.
                return (
                    f"<table cellpadding='0' cellspacing='0' style='border-collapse:separate;"
                    f"width:100%;min-width:200px;background:{bg};border:1px solid {border};{outline}"
                    f"border-radius:10px;overflow:hidden'>"
                    f"<tr>"
                    f"<td style='width:4px;background:{bar}' aria-hidden></td>"
                    f"<td style='padding:12px 14px;vertical-align:top'>"
                    # Top row: name on the left, status badge on the right.
                    f"<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%'><tr>"
                    f"<td style='vertical-align:top'>"
                    f"<div style='font-size:13px;font-weight:700;color:{fg};line-height:1.25;word-break:break-word'>{_esc(s_name)}</div>"
                    f"{type_html}"
                    f"</td>"
                    f"<td style='vertical-align:top;text-align:right;padding-left:8px;white-space:nowrap'>{status_badge}</td>"
                    f"</tr></table>"
                    f"{metrics_html}"
                    f"</td></tr></table>"
                )

            # ── Decide which layout: layered DAG vs. flat row ────
            steps_by_id: dict[str, Any] = {}
            for idx, s in enumerate(workflow_steps):
                if isinstance(s, dict):
                    sid = s.get("id") or s.get("name") or f"step{idx+1}"
                    if sid not in steps_by_id:
                        steps_by_id[sid] = s

            use_dag = (
                isinstance(workflow_connections, list)
                and len(workflow_connections) > 0
                and len(steps_by_id) > 1
            )

            if use_dag:
                # Build adjacency: downstream + upstream lookups
                # keyed by the same id we used for steps_by_id.
                # Accept both the canonical {from,to} shape and the
                # IR's {from_step,to_step} shape so callers don't
                # need to massage the payload.
                upstream: dict[str, list[str]] = {sid: [] for sid in steps_by_id}
                downstream: dict[str, list[str]] = {sid: [] for sid in steps_by_id}
                for c in workflow_connections:
                    if not isinstance(c, dict):
                        continue
                    f = c.get("from") or c.get("from_step")
                    t = c.get("to") or c.get("to_step")
                    if not f or not t or f == t:
                        continue
                    if f in steps_by_id and t in steps_by_id:
                        downstream[f].append(t)
                        upstream[t].append(f)

                # Compute rank = longest path from any source (memo + cycle cap).
                # A source is any step with no upstream. Cycle safety:
                # cap recursion depth at len(steps); on cap, return 0
                # so a cycle doesn't blow the stack. Real pipelines
                # are DAGs so this is purely defensive.
                rank: dict[str, int] = {}
                computing: set[str] = set()

                def _r(sid: str, depth: int = 0) -> int:
                    if sid in rank:
                        return rank[sid]
                    if depth > len(steps_by_id) or sid in computing:
                        rank[sid] = 0  # cycle break
                        return 0
                    computing.add(sid)
                    ups = upstream.get(sid, [])
                    r = 0 if not ups else max(_r(u, depth + 1) for u in ups) + 1
                    computing.discard(sid)
                    rank[sid] = r
                    return r

                for sid in steps_by_id:
                    _r(sid)

                # Group by rank, preserving the original step order
                # within each rank (matches what the user sees in the
                # editor — top-to-bottom of the canvas usually).
                by_rank: dict[int, list[str]] = {}
                seen_order = list(steps_by_id.keys())
                order_index = {sid: i for i, sid in enumerate(seen_order)}
                for sid, r in rank.items():
                    by_rank.setdefault(r, []).append(sid)
                for r in by_rank:
                    by_rank[r].sort(key=lambda s: order_index.get(s, 0))

                max_rank = max(rank.values()) if rank else 0

                # Render: one <td> per rank, each holding a stack of
                # cards (one nested <table> row per card). Between
                # columns: a thin → arrow column.
                #
                # Column-width math: cards are min-width:200px; arrows
                # are ~22px wide. Container has overflow-x:auto so
                # wide DAGs scroll instead of squishing.
                col_cells: list[str] = []
                for r_idx in range(max_rank + 1):
                    sids = by_rank.get(r_idx, [])
                    if not sids:
                        continue
                    card_rows: list[str] = []
                    for j, sid in enumerate(sids):
                        s = steps_by_id[sid]
                        # Vertical gap between stacked cards.
                        pad_top = "padding-top:12px;" if j > 0 else ""
                        card_rows.append(
                            f"<tr><td style='{pad_top}vertical-align:top'>"
                            f"{_render_step_card(s, order_index.get(sid, 0))}"
                            f"</td></tr>"
                        )
                    col_cells.append(
                        "<td style='padding:0 6px;vertical-align:middle;min-width:210px'>"
                        "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%'>"
                        + "".join(card_rows) +
                        "</table></td>"
                    )
                    if r_idx < max_rank:
                        # Only render the arrow if there's another non-
                        # empty column to its right (avoid trailing arrow
                        # on sparse rank sequences).
                        has_right = any(by_rank.get(rr) for rr in range(r_idx + 1, max_rank + 1))
                        if has_right:
                            col_cells.append(
                                "<td style='padding:0 6px;vertical-align:middle;color:#94a3b8;"
                                "font-size:22px;font-weight:300;text-align:center'>→</td>"
                            )

                lineage_inner = (
                    "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse'>"
                    "<tr>" + "".join(col_cells) + "</tr></table>"
                )
            else:
                # ── Flat row fallback (legacy callers, simple pipelines) ──
                # Single horizontal sequence in payload order. One arrow
                # between each pair. Indistinguishable from the pre-DAG
                # rendering so we don't regress simple-pipeline emails.
                boxes: list[str] = []
                for i, s in enumerate(workflow_steps):
                    boxes.append(
                        "<td style='padding:0 6px;vertical-align:top'>"
                        f"{_render_step_card(s, i)}"
                        "</td>"
                    )
                    if i < len(workflow_steps) - 1:
                        boxes.append(
                            "<td style='padding:0 4px;vertical-align:middle;color:#94a3b8;"
                            "font-size:22px;font-weight:300'>→</td>"
                        )
                lineage_inner = (
                    "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;margin:0 auto'>"
                    "<tr>" + "".join(boxes) + "</tr></table>"
                )

            lineage_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Lineage</div>"
                "<div style='overflow-x:auto;background:#ffffff;border:1px solid #e2e8f0;"
                "border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                + lineage_inner +
                "</div></div>"
            )

        # ── STEP PATH — breadcrumb to the failed step (complex pipelines) ──
        # Surfaces where in the DAG the failure happened — e.g.
        # "Pipeline > ForEach (customers) > IfBranch (active) > DBSink".
        # Without this, a failure inside a 3-deep loop says "DBSink
        # failed" with no context for *which* DBSink invocation.
        step_path_html = ""
        if isinstance(step_path, list) and len(step_path) > 1:
            crumbs = []
            for i, name in enumerate(step_path):
                is_last = (i == len(step_path) - 1)
                style = (
                    "padding:6px 12px;border-radius:6px;font-size:12px;font-weight:600;"
                    f"background:{'#fee2e2' if is_last else '#f1f5f9'};"
                    f"color:{'#991b1b' if is_last else '#475569'};"
                    f"border:1px solid {'#fca5a5' if is_last else '#e2e8f0'}"
                )
                crumbs.append(f"<span style='{style}'>{_esc(str(name))}</span>")
                if not is_last:
                    crumbs.append(
                        "<span style='color:#94a3b8;font-size:14px;margin:0 6px'>›</span>"
                    )
            step_path_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Failure Path</div>"
                "<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;"
                "padding:18px;display:flex;flex-wrap:wrap;align-items:center;"
                "box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                + "".join(crumbs) +
                "</div></div>"
            )

        # ── ITERATION CONTEXT — for loop failures ────────────────
        # When a step inside a foreach/until loop fails, recipients
        # need to know which iteration died. Otherwise debugging is
        # 'iterate over 100k records, find which one' guesswork.
        iteration_html = ""
        if iteration and (iteration.get("current") is not None or iteration.get("key")):
            loop_name = _esc(str(iteration.get("loop_name", "Loop")))
            current = iteration.get("current")
            total = iteration.get("total")
            key = iteration.get("key", "")
            progress_pct = None
            if isinstance(current, int) and isinstance(total, int) and total > 0:
                progress_pct = round((current / total) * 100, 1)
            cells = []
            cells.append(
                f"<div><div style='font-size:10px;font-weight:700;color:#64748b;"
                f"text-transform:uppercase;letter-spacing:.04em'>Loop</div>"
                f"<div style='font-size:14px;color:#0f172a;font-weight:600;margin-top:4px'>{loop_name}</div></div>"
            )
            if current is not None:
                pos_str = f"{current} of {total}" if total else str(current)
                pct_str = f" ({progress_pct}%)" if progress_pct is not None else ""
                cells.append(
                    f"<div><div style='font-size:10px;font-weight:700;color:#64748b;"
                    f"text-transform:uppercase;letter-spacing:.04em'>Iteration</div>"
                    f"<div style='font-size:14px;color:#0f172a;font-weight:600;margin-top:4px'>"
                    f"{_esc(pos_str)}{pct_str}</div></div>"
                )
            if key:
                cells.append(
                    f"<div><div style='font-size:10px;font-weight:700;color:#64748b;"
                    f"text-transform:uppercase;letter-spacing:.04em'>Key</div>"
                    f"<div style='font-size:13px;color:#0f172a;font-family:ui-monospace,Consolas,Monaco,monospace;"
                    f"margin-top:4px;word-break:break-all'>{_esc(str(key))}</div></div>"
                )
            iteration_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#92400e;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Iteration Context</div>"
                "<div style='background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:20px;"
                "display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;"
                "box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                + "".join(cells) +
                "</div></div>"
            )

        # ── ORIGINAL FAILURE — only when there's an error ─────────
        failure_html = ""
        if error_msg:
            badges: list[str] = []
            if failed_step:
                badges.append(
                    f"<span style='display:inline-block;padding:4px 12px;border-radius:6px;"
                    f"background:#fee2e2;color:#991b1b;font-size:11px;font-weight:700;"
                    f"margin-right:8px;letter-spacing:.02em'>Failed step: {_esc(failed_step)}</span>"
                )
            if attempts and isinstance(attempts, int) and attempts > 1:
                # Retry exhaustion is a different failure class than
                # 'failed first try' — surface it so operators don't
                # assume a transient and re-run blindly.
                badges.append(
                    f"<span style='display:inline-block;padding:4px 12px;border-radius:6px;"
                    f"background:#fef3c7;color:#92400e;font-size:11px;font-weight:700;"
                    f"margin-right:8px;letter-spacing:.02em'>"
                    f"Retried {attempts}× before giving up</span>"
                )
            badge_row = (
                f"<div style='margin-bottom:12px'>{''.join(badges)}</div>"
                if badges else ""
            )

            input_html = ""
            if input_snippet:
                # Show the input the failed step was called with so
                # someone reproducing the failure has the exact data.
                snippet = str(input_snippet)
                if len(snippet) > 800:
                    snippet = snippet[:800] + "\n…(truncated)"
                input_html = (
                    "<div style='margin-top:14px'>"
                    "<div style='font-size:10px;font-weight:700;color:#64748b;"
                    "text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px'>Step Input</div>"
                    f"<pre style='margin:0;font-family:ui-monospace,Consolas,Monaco,monospace;"
                    f"font-size:11.5px;color:#1e293b;background:#f8fafc;padding:10px 12px;"
                    f"border:1px solid #e2e8f0;border-radius:6px;white-space:pre-wrap;"
                    f"word-break:break-word;line-height:1.5'>{_esc(snippet)}</pre>"
                    "</div>"
                )

            failure_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#991b1b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Original Failure</div>"
                "<div style='background:#ffffff;border:1px solid #fecaca;border-left:4px solid #ef4444;"
                "border-radius:10px;padding:20px;box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                f"{badge_row}"
                f"<pre style='margin:0;font-family:ui-monospace,Consolas,Monaco,monospace;"
                f"font-size:12.5px;color:#7f1d1d;background:#fef2f2;padding:14px 16px;"
                f"border-radius:8px;white-space:pre-wrap;word-break:break-word;line-height:1.55;"
                f"overflow-x:auto'>"
                f"{_esc(error_msg)}</pre>"
                f"{input_html}"
                "</div></div>"
            )

        # ── LOOP PROGRESS — how far did the loop get before failure? ──
        # When the failed step lives inside a loop we want to show
        # 'completed 46 of 100, 1 failed, 53 not attempted' so the
        # operator knows whether to resume or re-run from scratch.
        loop_progress_html = ""
        if loop_progress:
            succ = int(loop_progress.get("successful_iterations") or 0)
            fail = int(loop_progress.get("failed_iterations") or 0)
            rem = int(loop_progress.get("remaining_iterations") or 0)
            done = succ + fail
            total = done + rem
            tiles = [
                ("#ecfdf5", "#065f46", "Completed", succ),
                ("#fef2f2", "#991b1b", "Failed", fail),
                ("#f8fafc", "#475569", "Remaining", rem),
            ]
            tile_html = "".join(
                f"<div style='flex:1;min-width:120px;background:{bg};color:{fg};"
                f"padding:14px 16px;border-radius:10px;text-align:center'>"
                f"<div style='font-size:24px;font-weight:700;line-height:1'>{count}</div>"
                f"<div style='font-size:11px;font-weight:600;margin-top:6px;"
                f"text-transform:uppercase;letter-spacing:.04em'>{label}</div></div>"
                for bg, fg, label, count in tiles
            )
            total_str = (
                f"<div style='text-align:right;font-size:11px;color:#64748b;margin-top:8px'>"
                f"Total iterations: <strong style='color:#0f172a'>{total}</strong></div>"
                if total > 0 else ""
            )
            loop_progress_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>Loop Progress</div>"
                f"<div style='display:flex;gap:12px;flex-wrap:wrap'>{tile_html}</div>"
                f"{total_str}"
                "</div>"
            )

        # ── EXECUTION TIMELINE — per-step duration + rows + status ──
        # A flat table of every step the executor ran, in order, with
        # metrics. Complements the high-level Lineage block by showing
        # exactly which steps cost time and which moved no rows.
        timeline_html = ""
        if isinstance(step_metrics, list) and step_metrics:
            TM_TH = (
                "padding:9px 12px;text-align:left;font-size:10px;font-weight:700;"
                "color:#475569;text-transform:uppercase;letter-spacing:.05em;"
                "background:#f1f5f9;border-bottom:1px solid #e2e8f0"
            )
            TM_TD = (
                "padding:10px 12px;font-size:12px;color:#0f172a;"
                "border-bottom:1px solid #f1f5f9"
            )
            rows_html = []
            for sm in step_metrics:
                if not isinstance(sm, dict):
                    continue
                name = _esc(str(sm.get("name", "")))
                indent = int(sm.get("depth", 0)) * 18  # nest visually
                indent_html = (
                    f"<span style='display:inline-block;width:{indent}px'></span>"
                    if indent else ""
                )
                row_status = str(sm.get("status", "")).lower()
                pill_b, pill_f = {
                    "success": ("#d1fae5", "#065f46"),
                    "error":   ("#fee2e2", "#991b1b"),
                    "failed":  ("#fee2e2", "#991b1b"),
                    "running": ("#dbeafe", "#1e40af"),
                    "skipped": ("#fef3c7", "#92400e"),
                }.get(row_status, ("#e2e8f0", "#475569"))
                status_pill = (
                    f"<span style='display:inline-block;padding:2px 8px;border-radius:9999px;"
                    f"background:{pill_b};color:{pill_f};font-size:10px;font-weight:700;"
                    f"text-transform:uppercase;letter-spacing:.03em'>"
                    f"{_esc(row_status or '—')}</span>"
                )
                dur_ms = sm.get("duration_ms")
                dur_str = f"{dur_ms / 1000:.1f}s" if isinstance(dur_ms, (int, float)) else "—"
                rows_in = sm.get("rows_in")
                rows_out = sm.get("rows_out")
                rows_str = (
                    f"{rows_in if rows_in is not None else '—'} → "
                    f"{rows_out if rows_out is not None else '—'}"
                )
                rows_html.append(
                    f"<tr><td style='{TM_TD}'>{indent_html}<strong>{name}</strong></td>"
                    f"<td style='{TM_TD}'>{status_pill}</td>"
                    f"<td style='{TM_TD};white-space:nowrap'>{dur_str}</td>"
                    f"<td style='{TM_TD};font-family:ui-monospace,Consolas,Monaco,monospace;"
                    f"font-size:11.5px;color:#475569;white-space:nowrap'>{rows_str}</td></tr>"
                )
            timeline_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#64748b;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>"
                "Execution Timeline</div>"
                "<div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;"
                "overflow:hidden;box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%'>"
                "<colgroup><col style='width:42%'><col style='width:14%'>"
                "<col style='width:14%'><col style='width:30%'></colgroup>"
                f"<tr><th style='{TM_TH}'>Step</th><th style='{TM_TH}'>Status</th>"
                f"<th style='{TM_TH}'>Duration</th><th style='{TM_TH}'>Rows in → out</th></tr>"
                + "".join(rows_html) +
                "</table></div></div>"
            )

        # ── RESUME HINT — when checkpoint exists, suggest restart ──
        # Especially useful for long-running batch jobs where re-
        # running from zero throws away hours of completed work.
        resume_html = ""
        if resume_available:
            token_html = (
                f"<code style='font-family:ui-monospace,Consolas,Monaco,monospace;"
                f"font-size:11px;background:#f1f5f9;padding:2px 8px;border-radius:4px;"
                f"color:#1e293b'>{_esc(str(resume_token))}</code>"
                if resume_token else ""
            )
            resume_html = (
                "<div style='margin-top:24px'>"
                "<div style='background:#eff6ff;border:1px solid #bfdbfe;border-left:4px solid #2563eb;"
                "border-radius:10px;padding:16px 20px'>"
                "<div style='font-size:12px;font-weight:700;color:#1e40af;"
                "text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px'>"
                "Resume Available</div>"
                "<div style='font-size:13px;color:#1e3a8a;line-height:1.5'>"
                "A checkpoint exists from this run. Restart will skip already-"
                "completed steps and resume from the failed point — no need to "
                f"re-process succeeded items.{(' Token: ' + token_html) if token_html else ''}"
                "</div></div></div>"
            )

        # ── AI DIAGNOSIS — mirrors the in-app indigo card ─────────
        ai_html = ""
        if ai_diagnosis or ai_suggestion:
            severity_pill = ""
            if ai_severity:
                sev_bg, sev_fg = ("#fee2e2", "#991b1b") if ai_severity == "error" else ("#fef3c7", "#92400e")
                severity_pill = (
                    f"<span style='display:inline-block;padding:1px 8px;border-radius:4px;"
                    f"background:{sev_bg};color:{sev_fg};font-size:10px;font-weight:700;"
                    f"text-transform:uppercase;margin-left:8px'>{_esc(ai_severity)}</span>"
                )
            # Header label distinguishes a real LLM diagnosis from the
            # rule-based fallback so recipients can calibrate trust.
            header_label = "AI Diagnosis" if ai_powered else "Rule-based Diagnosis"
            source_chip = (
                "<span style='display:inline-block;padding:1px 7px;border-radius:4px;"
                "background:#e0e7ff;color:#4338ca;font-size:9px;font-weight:700;"
                "text-transform:uppercase;letter-spacing:.04em;margin-left:8px'>LLM</span>"
                if ai_powered else
                "<span style='display:inline-block;padding:1px 7px;border-radius:4px;"
                "background:#f1f5f9;color:#475569;font-size:9px;font-weight:700;"
                "text-transform:uppercase;letter-spacing:.04em;margin-left:8px'>Pattern</span>"
            )
            diagnosis_row = (
                f"<div style='font-size:13px;font-weight:600;color:#1e293b;margin-bottom:6px'>{_esc(ai_diagnosis)}</div>"
                if ai_diagnosis else ""
            )
            suggestion_row = (
                f"<div style='font-size:13px;color:#334155'>"
                f"<span style='font-weight:600'>Suggestion: </span>{_esc(ai_suggestion)}</div>"
                if ai_suggestion else ""
            )
            ai_html = (
                "<div style='margin-top:24px'>"
                "<div style='font-size:11px;font-weight:700;color:#4338ca;"
                "text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px'>"
                f"{_esc(header_label)}{source_chip}{severity_pill}</div>"
                "<div style='background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;"
                "padding:20px;box-shadow:0 1px 3px rgba(15,23,42,.04)'>"
                f"{diagnosis_row}{suggestion_row}"
                "</div></div>"
            )

        # Container width: 920px works across Gmail / Outlook / Apple
        # Mail / mobile (responsive scales it down). 640px (the prior
        # value) wasted huge horizontal whitespace on desktop reads —
        # the table cells crammed up while the surrounding screen sat
        # empty. 920px gives the 5-column table room to breathe and
        # lets lineage + error blocks use the available space.
        # 2026-05-21: orange "F-Pulse OSS Alert" banner restored per
        # operator feedback (the visual anchor was useful). The redundant
        # sub-line `{workflow} — {STATUS}` underneath it is NOT restored
        # — that was the original redundancy with the compact header +
        # email subject. Banner now carries the brand mark only.
        return f"""<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             margin:0;padding:24px;background:#f1f5f9;color:#0f172a">
  <div style="max-width:920px;margin:0 auto">
    <div style="background:linear-gradient(135deg,#F5A623,#D4880A);padding:18px 28px;
                border-radius:12px 12px 0 0">
      <h2 style="color:#ffffff;margin:0;font-size:18px;font-weight:700;letter-spacing:.01em">
        F-Pulse OSS Alert
      </h2>
    </div>
    <div style="background:#fafafa;border:1px solid #e2e8f0;border-top:none;
                border-radius:0 0 12px 12px;padding:24px 28px">
      {pipeline_table}
      {run_details_html}
      {lineage_html}
      {step_path_html}
      {iteration_html}
      {failure_html}
      {loop_progress_html}
      {timeline_html}
      {resume_html}
      {ai_html}
    </div>
    <p style="font-size:11px;color:#94a3b8;margin-top:14px;text-align:center">
      Sent by F-Pulse OSS
    </p>
  </div>
</body>
</html>"""

    # ── Slack ──

    def _send_slack(self, rule: AlertRule, workflow: str, status: str, body: str) -> None:
        """Send Slack notification via incoming webhook."""
        url = rule.slack_webhook_url
        if not url:
            raise ValueError("No Slack webhook URL configured")

        # Slack status emoji
        emoji = ":white_check_mark:" if status == "success" else ":x:" if status in ("error", "failed") else ":warning:"

        payload = {
            "text": f"{emoji} *{workflow}* — {status.upper()}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{emoji} *{workflow}* — {status.upper()}\n```{body}```",
                    },
                },
            ],
        }

        self._post_json(url, payload)

    # ── Microsoft Teams ──

    def _send_teams(self, rule: AlertRule, workflow: str, status: str, body: str) -> None:
        """Send Teams notification via incoming webhook (Adaptive Card)."""
        url = rule.teams_webhook_url
        if not url:
            raise ValueError("No Teams webhook URL configured")

        color = "Good" if status == "success" else "Attention" if status in ("error", "failed") else "Warning"

        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "00B894" if status == "success" else "E74C3C",
            "summary": f"F-Pulse: {workflow} — {status}",
            "sections": [
                {
                    "activityTitle": f"F-Pulse OSS Alert: {workflow}",
                    "activitySubtitle": status.upper(),
                    "text": body.replace("\n", "<br>"),
                    "markdown": True,
                }
            ],
        }

        self._post_json(url, payload)

    # ── Generic Webhook ──

    def _send_webhook(self, rule: AlertRule, context: dict) -> None:
        """Send alert data as JSON POST to webhook URL."""
        url = rule.webhook_url
        if not url:
            raise ValueError("No webhook URL configured")

        payload = {
            "event": "pipeline_alert",
            "rule_id": rule.id,
            "rule_name": rule.name,
            "channel": rule.channel.value,
            "condition": rule.condition.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **context,
        }

        self._post_json(url, payload)

    # ── HTTP Helper ──

    @staticmethod
    def _post_json(url: str, payload: dict, timeout: int = 10) -> None:
        """POST JSON payload to a URL."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Webhook returned {resp.status}")
