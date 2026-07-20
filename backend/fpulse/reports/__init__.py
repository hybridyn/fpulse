"""F-Pulse System Inventory Reports.

Generates beautiful, industry-style documents describing the live state
of an F-Pulse installation — projects, pipelines, connections, users,
schedules, alerts, approval gates — for admin sign-off, auditor
review, or customer handover.

Two output formats:
  - Word (.docx) via python-docx
  - PDF via reportlab (Platypus flow engine)

Two scopes:
  - admin: full workspace view (every asset)
  - user:  ACL-filtered view (only what the caller can see)

Both are pure Python with no system dependencies — work cleanly on
Windows, macOS, and Linux.
"""

from fpulse.reports.inventory import InventoryCollector, InventoryReport

__all__ = ["InventoryCollector", "InventoryReport"]
