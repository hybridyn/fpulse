"""Render an InventoryReport to a PDF document.

Uses ReportLab Platypus (flow-based layout) — pure Python, no external
binaries, works identically on Windows, macOS, and Linux.

Layout mirrors the Word version:
  cover → TOC → executive summary → projects → connections → users →
  schedules → alerts → approval gates → appendix.
"""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer,
    Table, TableStyle,
)
from reportlab.pdfgen import canvas

from fpulse.reports.inventory import InventoryReport, ProjectInventory, PipelineInventory
from fpulse.reports.cron_human import cron_to_human


# Brand palette (hex → HexColor for ReportLab).
BRAND_DARK = colors.HexColor("#0F172A")
BRAND_VIOLET = colors.HexColor("#7C3AED")
BRAND_MUTED = colors.HexColor("#64748B")
BRAND_BG = colors.HexColor("#F8FAFC")
ACCENT_ROW = colors.HexColor("#F1F5F9")
STATUS_GREEN = colors.HexColor("#059669")
STATUS_AMBER = colors.HexColor("#D97706")
STATUS_RED = colors.HexColor("#DC2626")


def render_pdf(report: InventoryReport) -> bytes:
    """Produce the complete PDF as a bytes blob."""
    buf = io.BytesIO()
    styles = _build_styles()

    # Set up the document with a header/footer page template.
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        leftMargin=2.2 * cm, rightMargin=2.2 * cm,
        topMargin=2.2 * cm, bottomMargin=2.0 * cm,
        title=f"F-Pulse System Inventory — {report.workspace_name}",
        author=report.generated_by,
        subject="System inventory report",
    )

    frame = Frame(
        doc.leftMargin, doc.bottomMargin,
        doc.width, doc.height, id="normal",
    )
    doc.addPageTemplates([
        PageTemplate(
            id="cover", frames=frame,
            onPage=lambda c, d: None,  # clean cover, no header/footer
        ),
        PageTemplate(
            id="content", frames=frame,
            onPage=lambda c, d: _draw_header_footer(c, d, report),
        ),
    ])

    story: list = []

    # Cover page
    _add_cover(story, report, styles)
    story.append(PageBreak())
    # Switch to content template for the rest.
    from reportlab.platypus.doctemplate import NextPageTemplate
    story.append(NextPageTemplate("content"))

    is_free = report.tier == "free"

    _add_toc_stub(story, report, styles)
    story.append(PageBreak())
    # "What should I fix first?" headline — appears BEFORE the
    # executive summary so the reader sees actionable signal first
    # (May 6 2026 review: reports should drive decisions, not just describe).
    _add_insights_section(story, report, styles)
    # 2026-06-05 — Steward findings snapshot sits with the headline
    # insights (not buried later) so a reviewer sees the reliability
    # signal at the top of the report. Auto-skips if the Steward is
    # disabled in this workspace.
    _add_steward_section(story, report, styles)
    _add_executive_summary(story, report, styles)
    story.append(PageBreak())
    _add_operational_audit_section(story, report, styles)
    story.append(PageBreak())
    _add_failure_analysis_section(story, report, styles)
    story.append(PageBreak())
    _add_duration_analysis_section(story, report, styles)
    story.append(PageBreak())
    _add_projects_section(story, report, styles)
    story.append(PageBreak())
    _add_connections_section(story, report, styles)
    if not is_free:
        story.append(PageBreak())
        _add_users_section(story, report, styles)
    story.append(PageBreak())
    _add_schedules_section(story, report, styles)
    story.append(PageBreak())
    _add_alerts_section(story, report, styles)
    if not is_free:
        story.append(PageBreak())
        _add_approval_gates_section(story, report, styles)
    if is_free:
        story.append(PageBreak())
        _add_upgrade_cta(story, report, styles)
    story.append(PageBreak())
    _add_appendix(story, report, styles)

    doc.build(story)
    return buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════
# Styles
# ═══════════════════════════════════════════════════════════════════════


def _build_styles() -> dict:
    s = getSampleStyleSheet()
    return {
        "CoverTitle": ParagraphStyle(
            "CoverTitle", parent=s["Title"],
            fontSize=36, leading=44, alignment=TA_CENTER,
            textColor=BRAND_VIOLET, spaceAfter=6,
        ),
        "CoverSub": ParagraphStyle(
            "CoverSub", parent=s["Title"],
            fontSize=22, leading=28, alignment=TA_CENTER,
            textColor=BRAND_DARK, spaceAfter=20,
        ),
        "CoverMeta": ParagraphStyle(
            "CoverMeta", parent=s["BodyText"],
            fontSize=12, leading=16, alignment=TA_CENTER,
            textColor=BRAND_DARK,
        ),
        "CoverCaption": ParagraphStyle(
            "CoverCaption", parent=s["BodyText"],
            fontSize=9, leading=12, alignment=TA_CENTER,
            textColor=BRAND_MUTED, fontName="Helvetica-Oblique",
        ),
        "H1": ParagraphStyle(
            "H1", parent=s["Heading1"],
            fontSize=20, leading=26, textColor=BRAND_DARK,
            spaceBefore=0, spaceAfter=12, fontName="Helvetica-Bold",
        ),
        "H2": ParagraphStyle(
            "H2", parent=s["Heading2"],
            fontSize=15, leading=20, textColor=BRAND_VIOLET,
            spaceBefore=12, spaceAfter=8, fontName="Helvetica-Bold",
        ),
        "H3": ParagraphStyle(
            "H3", parent=s["Heading3"],
            fontSize=12, leading=16, textColor=BRAND_DARK,
            spaceBefore=10, spaceAfter=6, fontName="Helvetica-Bold",
        ),
        "H4": ParagraphStyle(
            "H4", parent=s["Heading4"],
            fontSize=11, leading=15, textColor=BRAND_DARK,
            spaceBefore=8, spaceAfter=4, fontName="Helvetica-Bold",
        ),
        "Body": ParagraphStyle(
            "Body", parent=s["BodyText"],
            fontSize=9.5, leading=13, textColor=BRAND_DARK,
            spaceAfter=6,
        ),
        "Muted": ParagraphStyle(
            "Muted", parent=s["BodyText"],
            fontSize=9, leading=12, textColor=BRAND_MUTED,
            fontName="Helvetica-Oblique", spaceAfter=4,
        ),
        "Cell": ParagraphStyle(
            "Cell", parent=s["BodyText"],
            fontSize=9, leading=12, textColor=BRAND_DARK,
            spaceAfter=0,
        ),
        "CellBold": ParagraphStyle(
            "CellBold", parent=s["BodyText"],
            fontSize=9, leading=12, textColor=BRAND_MUTED,
            fontName="Helvetica-Bold", spaceAfter=0,
        ),
    }


# ═══════════════════════════════════════════════════════════════════════
# Page decorations
# ═══════════════════════════════════════════════════════════════════════


def _draw_header_footer(c: canvas.Canvas, doc, report: InventoryReport) -> None:
    """Draw header + footer on content pages."""
    w, h = A4
    # Thin top accent strip
    c.setFillColor(BRAND_VIOLET)
    c.rect(0, h - 0.3 * cm, w, 0.3 * cm, stroke=0, fill=1)

    # Header text
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(BRAND_DARK)
    c.drawString(
        doc.leftMargin, h - 1.2 * cm,
        f"F-Pulse System Inventory — {report.workspace_name}",
    )
    c.setFont("Helvetica", 9)
    c.setFillColor(BRAND_MUTED)
    c.drawRightString(
        w - doc.rightMargin, h - 1.2 * cm,
        f"Generated {_fmt_iso(report.generated_at)}",
    )

    # Footer
    c.setFont("Helvetica", 8)
    c.setFillColor(BRAND_MUTED)
    c.drawString(
        doc.leftMargin, 1.2 * cm,
        f"Scope: {report.scope.upper()} · Schema v{report.schema_version}",
    )
    c.drawRightString(
        w - doc.rightMargin, 1.2 * cm,
        f"Page {doc.page}",
    )


# ═══════════════════════════════════════════════════════════════════════
# Cover
# ═══════════════════════════════════════════════════════════════════════


def _add_cover(story: list, report: InventoryReport, s: dict) -> None:
    is_free = report.tier == "free"
    title_line = "F-PULSE" if is_free else "F-PULSE+"
    subtitle = "System Report" if is_free else "System Inventory Report"

    story.append(Spacer(1, 4 * cm))
    # Tier-aware cover color — free = muted slate, plus = violet.
    if is_free:
        story.append(Paragraph(
            f"<font color='{_hex(BRAND_MUTED)}'>{title_line}</font>",
            s["CoverTitle"],
        ))
    else:
        story.append(Paragraph(title_line, s["CoverTitle"]))
    story.append(Paragraph(subtitle, s["CoverSub"]))
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        f"Workspace: {_esc(report.workspace_name)}", s["CoverMeta"],
    ))

    if report.env_filter != "all":
        env_color = _hex(STATUS_RED) if report.env_filter == "prod" else _hex(BRAND_VIOLET)
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph(
            f"<font color='{env_color}'><b>Environment: "
            f"{report.env_filter.upper()} only</b></font>",
            s["CoverMeta"],
        ))

    story.append(Spacer(1, 1 * cm))

    product_label = "F-Pulse version" if is_free else "F-Pulse version"
    meta_rows = [
        ["Generated at", _fmt_iso(report.generated_at)],
        ["Generated by", report.generated_by],
        ["Report scope",
         "Workspace (full)" if report.tier == "free"
         else ("Administrator (full workspace)" if report.scope == "admin"
               else "User (ACL-filtered)")],
        [product_label,
         f"{report.fpulse_version} (schema v{report.schema_version})"],
    ]
    tbl = Table(
        [[Paragraph(r[0], s["CellBold"]), Paragraph(r[1], s["Cell"])]
         for r in meta_rows],
        colWidths=[5 * cm, 8 * cm], hAlign="CENTER",
    )
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BRAND_MUTED),
    ]))
    story.append(tbl)

    story.append(Spacer(1, 3 * cm))
    # Redaction notice — trust signal, shown in green so readers see it.
    story.append(Paragraph(
        "<font color='#059669'><b>🔒 All credentials in this report are "
        "redacted. Secrets are shown as Vault references or marked "
        "[INLINE — MIGRATE]. This document is safe to share via email, "
        "ticketing, or print.</b></font>",
        s["CoverCaption"],
    ))
    story.append(Spacer(1, 0.8 * cm))
    story.append(Paragraph(
        "This report describes the live state of your F-Pulse installation "
        "at the moment of generation. For the current state, regenerate from "
        "the Reports page.",
        s["CoverCaption"],
    ))


def _add_toc_stub(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("Table of Contents", s["H1"]))
    is_free = report.tier == "free"
    rows = [
        ("1.", "Executive Summary"),
        ("2.", "Operational Audit"),
        ("3.", "Projects"),
        ("4.", "Connections"),
    ]
    if not is_free:
        rows.append(("5.", "Users"))
    rows.extend([
        ("6.", "Schedules"),
        ("7.", "Alert Rules"),
    ])
    if not is_free:
        rows.append(("8.", "Approval Gates"))
    rows.append(("A.", "Appendix — Report Metadata"))
    for num, title in rows:
        story.append(Paragraph(f"{num}  {title}", s["Body"]))


# ═══════════════════════════════════════════════════════════════════════
# Executive summary
# ═══════════════════════════════════════════════════════════════════════


def _add_executive_summary(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("1. Executive Summary", s["H1"]))
    story.append(Paragraph(
        f"This report describes the live state of workspace "
        f"<b>{_esc(report.workspace_name)}</b> in the F-Pulse installation "
        f"at {_fmt_iso(report.generated_at)}. All counts and lists are "
        "sourced directly from the backing stores at the time of "
        "generation.",
        s["Body"],
    ))

    totals = report.totals
    is_free = report.tier == "free"
    data = [
        ["Metric", "Count"],
        ["Projects", str(totals.get("projects", 0))],
        ["Pipelines (total)", str(totals.get("pipelines", 0))],
        ["  ↳ DEV only (not in PROD)", str(totals.get("pipelines_dev_only", 0))],
        ["  ↳ Live in PROD (deployed)", str(totals.get("pipelines_in_prod", 0))],
        ["Connections (total)", str(totals.get("connections", 0))],
        ["  ↳ DEV-scoped",     str(totals.get("connections_dev", 0))],
        ["  ↳ PROD-scoped",    str(totals.get("connections_prod", 0))],
        ["  ↳ Shared (unset)", str(totals.get("connections_shared", 0))],
        ["  ↳ With inline credentials (migrate candidates)",
         str(totals.get("connections_inline_creds", 0))],
    ]
    # Users + Approval Gates are Plus-only concepts.
    if not is_free:
        data.append(["Users (active / total)",
                     f"{totals.get('users_active', 0)} / {totals.get('users', 0)}"])
    data.extend([
        ["Schedules (enabled / total)",
         f"{totals.get('schedules_enabled', 0)} / {totals.get('schedules', 0)}"],
        ["Alert rules (enabled / total)",
         f"{totals.get('alerts_enabled', 0)} / {totals.get('alerts', 0)}"],
    ])
    if not is_free:
        data.append(["Approval gates", str(totals.get("approval_gates", 0))])
    tbl = Table(data, colWidths=[11 * cm, 5 * cm], hAlign="LEFT")
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)

    story.append(Spacer(1, 0.4 * cm))

    # Health
    story.append(Paragraph("1.1 Installation Health", s["H2"]))
    score = report.health.get("score", 0)
    score_color = (STATUS_GREEN if score >= 80
                   else STATUS_AMBER if score >= 50 else STATUS_RED)
    story.append(Paragraph(
        f"<b><font color='{_hex(score_color)}'>Health score: {score} / 100</font></b>",
        s["Body"],
    ))

    issues = report.health.get("issues", [])
    if not issues:
        story.append(Paragraph(
            "<font color='#059669'>No issues detected — system is in clean shape.</font>",
            s["Body"],
        ))
    else:
        story.append(Paragraph("Issues requiring attention:", s["Body"]))
        for issue in issues:
            story.append(Paragraph(f"• {_esc(issue)}", s["Body"]))


# ═══════════════════════════════════════════════════════════════════════
# Insights — "what should I fix first?" (May 6 2026)
# ═══════════════════════════════════════════════════════════════════════


def _add_insights_section(
    story: list, report: InventoryReport, s: dict,
) -> None:
    """Render the insights headline: a one-line top action + a count
    summary (failing / stale / healthy) + the most-actionable items.
    Intentionally compact — sits at the top of the report and gives
    the reader an answer to 'what now?' before they scroll."""
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors as rl_colors
    insights = report.insights
    if not insights or (insights.failing_count + insights.stale_count + insights.healthy_count) == 0:
        # Nothing to render — e.g. brand-new install with no published pipelines.
        return

    story.append(Paragraph(
        "<b>What should I fix first?</b>",
        s["H1"],
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(insights.top_action, s["Body"]))
    story.append(Spacer(1, 8))

    # KPI strip — failing / stale / healthy counts
    kpis = [
        [
            Paragraph("<b>FAILING</b>", s["Muted"]),
            Paragraph("<b>STALE</b>", s["Muted"]),
            Paragraph("<b>HEALTHY</b>", s["Muted"]),
        ],
        [
            Paragraph(f"<font size=14 color='#dc2626'><b>{insights.failing_count}</b></font>", s["Body"]),
            Paragraph(f"<font size=14 color='#d97706'><b>{insights.stale_count}</b></font>", s["Body"]),
            Paragraph(f"<font size=14 color='#059669'><b>{insights.healthy_count}</b></font>", s["Body"]),
        ],
    ]
    kpi_table = Table(kpis, colWidths=[160, 160, 160])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#f1f5f9")),
        ("BOX", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#cbd5e1")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 10))

    # Per-item details — one paragraph each. Skip if no detail.
    for item in insights.items:
        color = {
            "critical": "#dc2626",
            "warning": "#d97706",
            "ok": "#059669",
            "info": "#0369a1",
        }.get(item.severity, "#475569")
        line = (
            f"<font color='{color}'><b>{item.icon} {item.headline}</b></font>"
            + (f" — {item.detail}" if item.detail else "")
        )
        story.append(Paragraph(line, s["Body"]))
        story.append(Spacer(1, 3))

    story.append(Spacer(1, 12))


def _add_steward_section(
    story: list, report: InventoryReport, s: dict,
) -> None:
    """Render the Steward findings snapshot at report-generation time.

    Compact card mirroring the insights pattern: a headline, a 3-cell
    KPI strip (P1 / P2 / P3 counts), and a per-kind breakdown line.
    Skipped entirely if the Steward is disabled in this workspace OR
    if there are no findings (no signal = nothing to surface).
    """
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors as rl_colors

    sw = report.steward_summary or {}
    if not sw.get("enabled"):
        return
    total = sw.get("total_open_findings", 0)
    if total == 0:
        return  # clean workspace — nothing to surface

    by_sev = sw.get("by_severity") or {}
    by_kind = sw.get("by_kind") or {}
    mem = sw.get("memory_stats") or {}

    story.append(Paragraph(
        "<b>Steward — reliability snapshot</b>",
        s["H1"],
    ))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        f"The F-Pulse Steward (read-only reliability + learning layer) "
        f"sees <b>{total}</b> open finding{'s' if total != 1 else ''} "
        f"in this workspace at report time. "
        f"Open the eye icon in the app header for the full detail.",
        s["Body"],
    ))
    story.append(Spacer(1, 8))

    # Severity KPI strip
    kpis = [
        [
            Paragraph("<b>P1 (escalated)</b>", s["Muted"]),
            Paragraph("<b>P2 (review)</b>", s["Muted"]),
            Paragraph("<b>P3 (info)</b>", s["Muted"]),
        ],
        [
            Paragraph(f"<font size=14 color='#dc2626'><b>{by_sev.get('p1', 0)}</b></font>", s["Body"]),
            Paragraph(f"<font size=14 color='#d97706'><b>{by_sev.get('p2', 0)}</b></font>", s["Body"]),
            Paragraph(f"<font size=14 color='#475569'><b>{by_sev.get('p3', 0)}</b></font>", s["Body"]),
        ],
    ]
    kpi_table = Table(kpis, colWidths=[160, 160, 160])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#f5f3ff")),  # violet tint
        ("BOX", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#c4b5fd")),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#c4b5fd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 8))

    # By-kind breakdown — one line per finding kind
    if by_kind:
        kind_labels = {
            "duplicate_source": "Duplicate source",
            "duplicate_pipeline": "Duplicate pipeline",
            "failure_rca": "Failure RCA",
            "volume_anomaly": "Volume anomaly",
            "schema_drift": "Schema drift",
            "cost_recommendation": "Cost recommendation",
        }
        for kind, count in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            label = kind_labels.get(kind, kind)
            story.append(Paragraph(
                f"<font color='#6d28d9'><b>•</b></font> "
                f"<b>{label}</b>: {count}",
                s["Body"],
            ))

    # Memory journal footer — proves the learning layer has accumulated data
    scans = mem.get("total_scans", 0)
    dismisses = mem.get("total_dismisses", 0)
    resolves = mem.get("total_resolves", 0)
    if scans or dismisses or resolves:
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"<i>Learning history: {scans} scans recorded · "
            f"{dismisses} dismissed (intentional) · "
            f"{resolves} resolved by user action.</i>",
            s["Muted"],
        ))
    story.append(Spacer(1, 12))


# ═══════════════════════════════════════════════════════════════════════
# Operational Audit (section 2)
# ═══════════════════════════════════════════════════════════════════════


def _add_duration_analysis_section(
    story: list, report: InventoryReport, s: dict,
) -> None:
    """30-day duration rollup. Sorted by p95 desc so the slowest
    pipelines top the list. Flags last-run regressions."""
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors as rl_colors
    da = report.duration_analysis
    story.append(Paragraph("<b>Run duration analysis (last 30 days)</b>", s["H1"]))
    story.append(Spacer(1, 6))
    if not da or da.total_pipelines == 0:
        story.append(Paragraph(
            "No pipelines with ≥ 3 runs in the last 30 days yet — duration "
            "analysis needs a minimum sample size to be meaningful.",
            s["Body"],
        ))
        return
    summary = (
        f"<b>{da.total_pipelines}</b> pipelines, "
        f"<b>{da.total_runs}</b> runs total."
    )
    if da.slowest_pipeline:
        summary += f" Slowest by p95: <b>{da.slowest_pipeline}</b>."
    if da.regressions_count:
        summary += (
            f" <font color='#dc2626'><b>{da.regressions_count}</b></font> "
            f"pipeline{'s' if da.regressions_count != 1 else ''} ran ≥1.5× their "
            f"average on the most recent execution (regression suspect)."
        )
    story.append(Paragraph(summary, s["Body"]))
    story.append(Spacer(1, 10))

    rows = [[
        Paragraph("<b>Pipeline</b>", s["CellBold"]),
        Paragraph("<b>Runs</b>", s["CellBold"]),
        Paragraph("<b>Avg</b>", s["CellBold"]),
        Paragraph("<b>p95</b>", s["CellBold"]),
        Paragraph("<b>Last</b>", s["CellBold"]),
        Paragraph("<b>Note</b>", s["CellBold"]),
    ]]
    for r in da.rows[:15]:
        note = ""
        if r.regression:
            note = "<font color='#d97706'><b>regression</b></font>"
        rows.append([
            Paragraph(r.pipeline_name, s["Cell"]),
            Paragraph(str(r.runs), s["Cell"]),
            Paragraph(f"{r.avg_ms:,} ms", s["Cell"]),
            Paragraph(f"{r.p95_ms:,} ms", s["Cell"]),
            Paragraph(f"{r.last_ms:,} ms", s["Cell"]),
            Paragraph(note or "—", s["Cell"]),
        ])
    t = Table(rows, colWidths=[180, 50, 70, 70, 70, 80])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#f1f5f9")),
        ("BOX", (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)


def _add_failure_analysis_section(
    story: list, report: InventoryReport, s: dict,
) -> None:
    """30-day failure analysis: top failing pipelines, most common
    error patterns, per-day failure trend. Renders nothing when the
    workspace has no failures in the window — keeps clean installs
    from showing empty tables."""
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors as rl_colors
    fa = report.failure_analysis
    if not fa or fa.total_failures == 0:
        story.append(Paragraph("<b>Failure analysis (last 30 days)</b>", s["H1"]))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "No failures in the last 30 days — pipelines are running clean.",
            s["Body"],
        ))
        return

    story.append(Paragraph("<b>Failure analysis (last 30 days)</b>", s["H1"]))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"<b>{fa.total_failures}</b> failures across "
        f"<b>{fa.unique_failing_pipelines}</b> pipeline"
        f"{'s' if fa.unique_failing_pipelines != 1 else ''} in the last "
        f"{fa.window_days} days.",
        s["Body"],
    ))
    story.append(Spacer(1, 10))

    # Top failing pipelines table
    if fa.top_failing:
        story.append(Paragraph("<b>Top failing pipelines</b>", s["H2"]))
        rows = [[
            Paragraph("<b>Pipeline</b>", s["CellBold"]),
            Paragraph("<b>Failures</b>", s["CellBold"]),
            Paragraph("<b>Rate</b>", s["CellBold"]),
            Paragraph("<b>Last failure</b>", s["CellBold"]),
        ]]
        for r in fa.top_failing[:10]:
            rate_str = f"{r.failure_rate_pct:.0f}% of {r.total_runs}" if r.total_runs else "—"
            rows.append([
                Paragraph(r.pipeline_name, s["Cell"]),
                Paragraph(str(r.failure_count), s["Cell"]),
                Paragraph(rate_str, s["Cell"]),
                Paragraph(_fmt_iso(r.last_failure_at) or "—", s["Cell"]),
            ])
        t = Table(rows, colWidths=[200, 60, 90, 130])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#fee2e2")),
            ("BOX", (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # Most common error patterns table
    if fa.top_errors:
        story.append(Paragraph("<b>Most common error patterns</b>", s["H2"]))
        rows = [[
            Paragraph("<b>Error signature</b>", s["CellBold"]),
            Paragraph("<b>Count</b>", s["CellBold"]),
            Paragraph("<b>Affected pipelines</b>", s["CellBold"]),
        ]]
        for r in fa.top_errors[:8]:
            affected = ", ".join(r.affected_pipelines[:4])
            if len(r.affected_pipelines) > 4:
                affected += f" + {len(r.affected_pipelines) - 4} more"
            rows.append([
                Paragraph(r.error_signature[:120], s["Cell"]),
                Paragraph(str(r.count), s["Cell"]),
                Paragraph(affected or "—", s["Cell"]),
            ])
        t = Table(rows, colWidths=[260, 50, 170])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#f1f5f9")),
            ("BOX", (0, 0), (-1, -1), 0.4, rl_colors.HexColor("#cbd5e1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, rl_colors.HexColor("#e2e8f0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 12))

    # Per-day trend — render as a simple bar via printable bullet length.
    # Avoids pulling in a charting dependency just for one section.
    if fa.failures_by_day:
        story.append(Paragraph("<b>Failure trend (per day)</b>", s["H2"]))
        max_v = max((d.get("failures", 0) for d in fa.failures_by_day), default=0)
        if max_v > 0:
            for d in fa.failures_by_day[-14:]:
                n = d.get("failures", 0)
                bar = "█" * max(0, int(round(n / max_v * 24)))
                line = (
                    f"<font face='Helvetica' color='#94a3b8'>{d.get('date','')}</font>"
                    f"  <font color='#dc2626'>{bar}</font>"
                    f"  <font color='#475569'>{n}</font>"
                )
                story.append(Paragraph(line, s["Body"]))
        else:
            story.append(Paragraph("Trend is flat — no failures in the window.", s["Body"]))
        story.append(Spacer(1, 12))


def _add_operational_audit_section(
    story: list, report: InventoryReport, s: dict,
) -> None:
    audit = report.operational_audit
    story.append(Paragraph("2. Operational Audit", s["H1"]))
    story.append(Paragraph(
        "Day-to-day operational signals: execution health over the last "
        f"{audit.window_hours} hours, pipelines that failed last run, and "
        "the next scheduled firings. This section is intended for data-ops "
        "teams checking system status at a glance.",
        s["Body"],
    ))

    # 2.1 Execution health
    story.append(Paragraph("2.1 Execution health (last 24 hours)", s["H2"]))
    rate_str = (f"{audit.success_rate_pct:.1f} %"
                if audit.total_executions else "— (no runs)")
    health_rows = [
        ["Total executions", str(audit.total_executions)],
        ["Successful", str(audit.successful_executions)],
        ["Failed / timed out", str(audit.failed_executions)],
        ["Success rate", rate_str],
        ["Average duration",
         f"{audit.avg_duration_ms} ms" if audit.avg_duration_ms else "—"],
    ]
    data = [["Metric", "Value"]] + health_rows
    tbl = Table(data, colWidths=[8 * cm, 7 * cm], hAlign="LEFT")
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)

    # 2.2 Recent failures
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("2.2 Pipelines whose last run failed", s["H2"]))
    if not audit.recent_failures:
        story.append(Paragraph(
            "<font color='#059669'>No pipelines are currently in a failed state.</font>",
            s["Body"],
        ))
    else:
        data = [["Pipeline", "Failed at", "Error"]]
        for f in audit.recent_failures:
            data.append([
                Paragraph(_esc(f.workflow_name), s["Cell"]),
                Paragraph(_fmt_iso(f.failed_at), s["Cell"]),
                Paragraph(_esc((f.error or "—")[:120]), s["Cell"]),
            ])
        tbl = Table(data, colWidths=[5 * cm, 3.5 * cm, 7.5 * cm],
                    hAlign="LEFT", repeatRows=1)
        tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
        story.append(tbl)

    # 2.3 Next scheduled runs
    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("2.3 Next scheduled runs", s["H2"]))
    if not audit.next_runs:
        story.append(Paragraph("No upcoming scheduled runs.", s["Muted"]))
    else:
        data = [["Pipeline", "Schedule", "Env", "Next fire (UTC)"]]
        for nr in audit.next_runs:
            # 2026-05-26 — humanize cron so users see "Every 2 minutes"
            # instead of "*/2 * * * *". Raw expression on a second line in
            # a small muted font preserves it for engineers debugging
            # schedules.
            human = cron_to_human(nr.cron_expression)
            cron_cell = (
                f"{_esc(human)}<br/>"
                f"<font size='7' color='#94A3B8'>{_esc(nr.cron_expression)}</font>"
                if human != nr.cron_expression
                else _esc(nr.cron_expression)
            )
            data.append([
                Paragraph(_esc(nr.workflow_name), s["Cell"]),
                Paragraph(cron_cell, s["Cell"]),
                Paragraph(_esc(nr.environment), s["Cell"]),
                Paragraph(_fmt_iso(nr.next_fire_at), s["Cell"]),
            ])
        tbl = Table(data, colWidths=[5 * cm, 3.5 * cm, 2 * cm, 5.5 * cm],
                    hAlign="LEFT", repeatRows=1)
        tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
        story.append(tbl)


# ═══════════════════════════════════════════════════════════════════════
# Projects (section 3)
# ═══════════════════════════════════════════════════════════════════════


def _add_projects_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("3. Projects", s["H1"]))

    if not report.projects:
        story.append(Paragraph("No projects visible at the report's scope.",
                               s["Muted"]))
        return

    story.append(Paragraph(
        f"This installation has {len(report.projects)} project(s). Each "
        "project below lists its owner, approver, members, and the pipelines "
        "inside it with their connections, schedules, alerts, and recent runs.",
        s["Body"],
    ))

    for i, proj in enumerate(report.projects, start=1):
        _add_project_block(story, proj, f"3.{i}", s)


def _add_project_block(story: list, proj: ProjectInventory, section: str, s: dict) -> None:
    story.append(Paragraph(f"{section} {_esc(proj.name)}", s["H2"]))

    meta = [
        ["Project ID", proj.id],
        ["Description", proj.description or "—"],
        ["Owner", proj.owner or "(unset)"],
        ["Approver", proj.approver or "(all admins)"],
        ["Approval status", proj.approval_status or "none"],
        ["Members", ", ".join(proj.member_names) if proj.member_names else "—"],
        ["Created", _fmt_iso(proj.created_at)],
        ["Pipelines in project", str(proj.pipeline_count)],
    ]
    tbl = Table(
        [[Paragraph(r[0], s["CellBold"]), Paragraph(_esc(r[1]), s["Cell"])]
         for r in meta],
        colWidths=[4 * cm, 12.5 * cm], hAlign="LEFT",
    )
    tbl.setStyle(_kv_style())
    story.append(tbl)
    story.append(Spacer(1, 0.3 * cm))

    if not proj.pipelines:
        story.append(Paragraph("No pipelines in this project yet.", s["Muted"]))
        return

    story.append(Paragraph(f"{section}.1 Pipelines", s["H3"]))
    for j, p_inv in enumerate(proj.pipelines, start=1):
        _add_pipeline_block(story, p_inv, f"{section}.1.{j}", s)


def _add_pipeline_block(story: list, p: PipelineInventory, section: str, s: dict) -> None:
    story.append(Paragraph(f"{section} {_esc(p.name)}", s["H4"]))

    # ── Signals line: env badges + last-run pill + next-run (gap 4) ───
    env_chips = " ".join(
        f"<font color='{'#DC2626' if env == 'PROD' else '#7C3AED'}'><b>[{env}]</b></font>"
        for env in p.environments
    )
    lrs = (p.last_run_status or "").lower()
    if lrs == "success":
        run_pill = "<font color='#059669'><b>LAST RUN: SUCCESS</b></font>"
    elif lrs in ("error", "failed", "timeout"):
        run_pill = f"<font color='#DC2626'><b>LAST RUN: {lrs.upper()}</b></font>"
    elif lrs:
        run_pill = f"<font color='#D97706'><b>LAST RUN: {lrs.upper()}</b></font>"
    else:
        run_pill = "<font color='#64748B'><b>NEVER RUN</b></font>"
    last_at_part = (f" <font color='#64748B' size='8'>"
                    f"({_fmt_iso(p.last_run_at)})</font>"
                    if p.last_run_at else "")
    next_part = (f"  &nbsp; <b>NEXT:</b> {_fmt_iso(p.next_run_at)}"
                 if p.next_run_at else "")
    story.append(Paragraph(
        f"{env_chips} &nbsp; {run_pill}{last_at_part}{next_part}",
        s["Body"],
    ))

    # ── Purpose (the "why") — prominent, right under the name ──────────
    if p.business_purpose:
        story.append(Paragraph(
            f"<b>Purpose:</b> {_esc(p.business_purpose)}", s["Body"],
        ))

    rows = [
        ["Environment", " + ".join(p.environments)],
        ["Status", _status_label(p.status, p.deployed_version)],
        ["Latest version", f"v{p.latest_version}"],
        ["Deployed version",
         f"v{p.deployed_version}" if p.deployed_version else "not deployed"],
        ["Owner", p.owner or "—"],
    ]
    if p.description:
        rows.append(["Description", _esc(p.description)])
    if p.readme:
        rows.append(["Notes (README)", _esc(p.readme).replace("\n", "<br/>")])
    if p.approval_status:
        approval = p.approval_status
        if p.approved_by:
            approval += f" by {p.approved_by}"
        if p.submitted_by:
            approval += f" — submitted by {p.submitted_by}"
        rows.append(["Approval", approval])

    rows.append(["Nodes", f"{p.step_count} steps ({len(p.node_types)} types)"])
    if p.node_types:
        rows.append(["Node types", ", ".join(p.node_types)])

    if p.connections_used:
        lines = [f"{c['name']} ({c['type']})" for c in p.connections_used]
        rows.append(["Connections used", "; ".join(lines)])

    if p.schedules:
        sch = []
        for sch_row in p.schedules:
            enabled = "✓" if sch_row.get("enabled") else "✗"
            raw_cron = sch_row.get("cron", "?")
            sch.append(
                f"{enabled} {cron_to_human(raw_cron)} "
                f"({sch_row.get('timezone', 'UTC')}, "
                f"{sch_row.get('environment', 'DEV')})"
            )
        rows.append(["Schedules", "<br/>".join(sch)])
    else:
        rows.append(["Schedules", "none"])

    if p.alert_rules:
        alerts = []
        for a in p.alert_rules:
            enabled = "✓" if a.get("enabled") else "✗"
            channels = ", ".join(a.get("channels", [])) or "—"
            alerts.append(
                f"{enabled} {_esc(a.get('name', '(unnamed)'))} "
                f"on {_esc(a.get('condition', '?'))} → {_esc(channels)}"
            )
        rows.append(["Alert rules", "<br/>".join(alerts)])
    else:
        rows.append(["Alert rules", "none"])

    if p.last_runs:
        run_lines = []
        for r in p.last_runs:
            run_lines.append(
                f"{_esc(r.get('status', '?'))} | {r.get('duration_ms', 0)} ms | "
                f"{r.get('rows', 0)} rows | "
                f"{_fmt_iso(r.get('started_at', ''))} | "
                f"by {_esc(r.get('triggered_by', '—'))}"
            )
        rows.append([f"Last {len(run_lines)} runs", "<br/>".join(run_lines)])
    else:
        rows.append(["Recent runs", "no executions recorded"])

    if p.content_hash:
        rows.append(["Content hash",
                     p.content_hash[:16] + "… (signed artifact)"])

    tbl = Table(
        [[Paragraph(r[0], s["CellBold"]), Paragraph(str(r[1]), s["Cell"])]
         for r in rows],
        colWidths=[4 * cm, 12.5 * cm], hAlign="LEFT",
    )
    tbl.setStyle(_kv_style())
    story.append(tbl)
    story.append(Spacer(1, 0.3 * cm))


# ═══════════════════════════════════════════════════════════════════════
# Connections
# ═══════════════════════════════════════════════════════════════════════


def _add_connections_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("4. Connections", s["H1"]))

    if not report.connections:
        story.append(Paragraph("No connections configured.", s["Muted"]))
        return

    story.append(Paragraph(
        f"This installation has {len(report.connections)} saved connection(s). "
        "Connections are reusable references to data sources and sinks; each "
        "holds non-secret config plus a pointer to credentials in the Vault.",
        s["Body"],
    ))

    data = [["Name", "Type", "Env", "Capabilities", "Creds (redacted)", "Used by"]]
    for c in report.connections:
        # Redaction marker (gap 2) — Vault pointer masked, inline creds
        # explicitly called out so a reader knows it's a migrate candidate.
        if c.has_credential_ref:
            creds = f"Vault: {c.credential_ref}" if c.credential_ref else "Vault (redacted)"
        elif c.has_inline_creds:
            creds = "[INLINE — MIGRATE]"
        else:
            creds = "none"
        used = c.used_by_pipelines
        used_str = (", ".join(used[:3])
                    + (f" (+{len(used) - 3})" if len(used) > 3 else "")
                    if used else "(unused)")
        data.append([
            Paragraph(_esc(c.name), s["Cell"]),
            Paragraph(_esc(c.type), s["Cell"]),
            Paragraph(_esc(c.environment or "—"), s["Cell"]),
            Paragraph(_esc(", ".join(c.capabilities) or "—"), s["Cell"]),
            Paragraph(_esc(creds), s["Cell"]),
            Paragraph(_esc(used_str), s["Cell"]),
        ])
    tbl = Table(data, colWidths=[3.5 * cm, 2.5 * cm, 1.8 * cm, 3.0 * cm, 2.4 * cm, 3.3 * cm], hAlign="LEFT", repeatRows=1)
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)


# ═══════════════════════════════════════════════════════════════════════
# Users
# ═══════════════════════════════════════════════════════════════════════


def _add_users_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("5. Users", s["H1"]))

    if not report.users:
        story.append(Paragraph("No users found.", s["Muted"]))
        return

    story.append(Paragraph(
        f"{len(report.users)} user account(s) exist in this installation. "
        "Roles determine what each user can do; see the Security & Compliance "
        "guide for the full role matrix.",
        s["Body"],
    ))

    # Rollup
    by_role: dict[str, int] = {}
    for u in report.users:
        by_role[u.role] = by_role.get(u.role, 0) + 1

    story.append(Paragraph("5.1 By role", s["H2"]))
    data = [["Role", "Count"]]
    for role, count in sorted(by_role.items()):
        data.append([role, str(count)])
    tbl = Table(data, colWidths=[6 * cm, 3 * cm], hAlign="LEFT")
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)

    story.append(Spacer(1, 0.3 * cm))
    story.append(Paragraph("5.2 Roster", s["H2"]))

    data = [["Name", "Email", "Role", "Active", "Last login"]]
    for u in report.users:
        data.append([
            Paragraph(_esc(u.name or "—"), s["Cell"]),
            Paragraph(_esc(u.email or "—"), s["Cell"]),
            Paragraph(u.role, s["Cell"]),
            Paragraph("yes" if u.is_active else "<b>NO</b>", s["Cell"]),
            Paragraph(_fmt_iso(u.last_login_at) if u.last_login_at else "—", s["Cell"]),
        ])
    tbl = Table(data, colWidths=[4 * cm, 5 * cm, 2.5 * cm, 1.5 * cm, 3.5 * cm],
                hAlign="LEFT", repeatRows=1)
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)


# ═══════════════════════════════════════════════════════════════════════
# Schedules / Alerts / Gates
# ═══════════════════════════════════════════════════════════════════════


def _add_schedules_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("6. Schedules", s["H1"]))
    if not report.schedules:
        story.append(Paragraph("No schedules configured.", s["Muted"]))
        return
    story.append(Paragraph(
        f"{len(report.schedules)} schedule(s) configured. PROD schedules "
        "execute the pipeline's pinned deployed_version; DEV schedules execute "
        "the latest saved version.",
        s["Body"],
    ))
    data = [["Pipeline", "Schedule", "Timezone", "Env", "Next fire"]]
    for sch in report.schedules:
        # 2026-05-26 — see audit.next_runs renderer above for rationale.
        human = cron_to_human(sch.cron_expression)
        cron_cell = (
            f"{_esc(human)}<br/>"
            f"<font size='7' color='#94A3B8'>{_esc(sch.cron_expression)}</font>"
            if human != sch.cron_expression
            else _esc(sch.cron_expression)
        )
        data.append([
            Paragraph(_esc(sch.workflow_name or sch.workflow_id), s["Cell"]),
            Paragraph(cron_cell, s["Cell"]),
            Paragraph(_esc(sch.timezone), s["Cell"]),
            Paragraph(_esc(sch.environment), s["Cell"]),
            Paragraph(_fmt_iso(sch.next_fire_at) if sch.next_fire_at else "—", s["Cell"]),
        ])
    tbl = Table(data, colWidths=[5 * cm, 3.5 * cm, 2.5 * cm, 1.5 * cm, 3.5 * cm],
                hAlign="LEFT", repeatRows=1)
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)


def _add_alerts_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("7. Alert Rules", s["H1"]))
    if not report.alerts:
        story.append(Paragraph("No alert rules configured.", s["Muted"]))
        return
    story.append(Paragraph(
        f"{len(report.alerts)} alert rule(s) configured.",
        s["Body"],
    ))
    data = [["Rule", "Pipeline", "Condition", "Channels", "On"]]
    for a in report.alerts:
        data.append([
            Paragraph(_esc(a.name), s["Cell"]),
            Paragraph(_esc(a.workflow_name or a.workflow_id), s["Cell"]),
            Paragraph(_esc(a.condition), s["Cell"]),
            Paragraph(_esc(", ".join(a.channels) or "—"), s["Cell"]),
            Paragraph("✓" if a.enabled else "✗", s["Cell"]),
        ])
    tbl = Table(data, colWidths=[3.5 * cm, 4 * cm, 3.5 * cm, 3.5 * cm, 1.5 * cm],
                hAlign="LEFT", repeatRows=1)
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)


def _add_approval_gates_section(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("8. Approval Gates", s["H1"]))
    if not report.approval_gates:
        story.append(Paragraph(
            "No approval gates configured. PROD deploys fall back to notifying "
            "all admins and leads in the workspace. For tighter governance, "
            "configure per-project or per-pipeline gates.",
            s["Body"],
        ))
        return
    story.append(Paragraph(
        f"{len(report.approval_gates)} approval gate(s) configured. Gates "
        "resolve most-specific-wins: pipeline → project → global.",
        s["Body"],
    ))
    data = [["Scope", "Scope ID", "Min approvals", "Approvers", "Channels"]]
    for g in report.approval_gates:
        data.append([
            Paragraph(_esc(g.scope), s["Cell"]),
            Paragraph(_esc(g.scope_id or "(global)"), s["Cell"]),
            Paragraph(str(g.min_approvals), s["Cell"]),
            Paragraph(_esc(", ".join(g.approvers) or "—"), s["Cell"]),
            Paragraph(_esc(", ".join(g.notify_channels) or "—"), s["Cell"]),
        ])
    tbl = Table(data, colWidths=[2.5 * cm, 4 * cm, 2 * cm, 4.5 * cm, 3 * cm],
                hAlign="LEFT", repeatRows=1)
    tbl.setStyle(_grid_style(header_bg=BRAND_VIOLET, header_fg=colors.white))
    story.append(tbl)


# ═══════════════════════════════════════════════════════════════════════
# Appendix
# ═══════════════════════════════════════════════════════════════════════


def _add_upgrade_cta(story: list, report: InventoryReport, s: dict) -> None:
    """Free-tier only — a friendly, non-spammy callout describing what
    F-Pulse adds on top of this report."""
    story.append(Paragraph("Upgrade to F-Pulse", s["H1"]))
    story.append(Paragraph(
        "The report you are reading covers the core F-Pulse inventory. "
        "F-Pulse extends this report with enterprise-grade sections that "
        "are not available on the free tier:",
        s["Body"],
    ))

    upgrade_items = [
        ("User roster & role matrix",
         "Every user, their role, last login, and environment permissions."),
        ("Approval gates",
         "Per-pipeline, per-project, and global gates with approver lists."),
        ("Operational audit (full)",
         "24-hour success rate across every execution, with failure roster "
         "and the next 10 scheduled firings in one place."),
        ("Signed artifacts & audit log",
         "Tamper-evident pipeline deploys with SHA-256 content hashes and "
         "a full audit of every state-changing action."),
        ("Vault-backed credentials",
         "AES-256 encrypted secret storage with rotation, audit, and "
         "masked references in reports."),
        ("DEV → PROD approval workflow",
         "Submit, review, approve, deploy, rollback — all tracked."),
        ("Drift detection & retention",
         "Nightly schema-drift scans + two-tier retention with Parquet "
         "archive before purge."),
        ("Prometheus metrics, Grafana dashboards, Loki logs",
         "Production-grade observability, auto-provisioned."),
    ]
    for title, desc in upgrade_items:
        story.append(Paragraph(
            f"• <font color='{_hex(BRAND_VIOLET)}'><b>{_esc(title)}</b></font> — "
            f"{_esc(desc)}",
            s["Body"],
        ))

    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "Contact your F-Pulse account representative or visit the Admin "
        "page inside the application to activate a Plus license.",
        s["Muted"],
    ))


def _add_appendix(story: list, report: InventoryReport, s: dict) -> None:
    story.append(Paragraph("Appendix A — Report Metadata", s["H1"]))
    rows = [
        ["Generated at", _fmt_iso(report.generated_at)],
        ["Generated by", report.generated_by],
        ["Scope", report.scope],
        ["Tier", report.tier.upper()],
        ["Environment filter",
         "all environments" if report.env_filter == "all"
         else f"{report.env_filter.upper()} only"],
        ["Workspace ID", report.workspace_id],
        ["Product version",
         f"{'F-Pulse' if report.tier == 'free' else 'F-Pulse'} {report.fpulse_version}"],
        ["Schema version", f"v{report.schema_version}"],
        ["Report format", "PDF"],
    ]
    tbl = Table(
        [[Paragraph(r[0], s["CellBold"]), Paragraph(_esc(r[1]), s["Cell"])]
         for r in rows],
        colWidths=[5 * cm, 11 * cm], hAlign="LEFT",
    )
    tbl.setStyle(_kv_style())
    story.append(tbl)
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(
        "This report was generated by F-Pulse from live data and reflects the "
        "state of the system at the timestamp above. Regenerate from the "
        "Reports page in the F-Pulse UI at any time.",
        s["Muted"],
    ))


# ═══════════════════════════════════════════════════════════════════════
# Table styles
# ═══════════════════════════════════════════════════════════════════════


def _grid_style(*, header_bg, header_fg) -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), header_fg),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, -1), BRAND_DARK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ACCENT_ROW]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.25, BRAND_MUTED),
    ])


def _kv_style() -> TableStyle:
    return TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, BRAND_MUTED),
        ("BACKGROUND", (0, 0), (0, -1), BRAND_BG),
    ])


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _fmt_iso(s: str) -> str:
    if not s:
        return "—"
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return s


def _esc(s) -> str:
    """Escape < > & for ReportLab's Paragraph mini-markup."""
    if s is None:
        return "—"
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _status_label(status: str, deployed: int | None) -> str:
    base = (status or "draft").lower()
    if deployed:
        return f"{base.upper()} (PROD v{deployed})"
    return base.upper()


def _hex(color) -> str:
    """Convert a reportlab HexColor to a CSS-style hex string (#rrggbb).

    ReportLab's `<font color=...>` paragraph markup parses CSS colour
    values; a bare ``64748b`` is rejected as "Invalid color value", so
    the leading ``#`` is mandatory.
    """
    try:
        return "#" + color.hexval()[2:]  # 0xRRGGBB → #RRGGBB
    except AttributeError:
        return "#0F172A"
