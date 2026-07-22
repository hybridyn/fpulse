"""Draft-connector store — the inert "proposed connector" lifecycle.

The AI Copilot can DRAFT a connector (from an OpenAPI spec/URL or from sample
API responses) but it must never silently create a live connector. A draft is
**inert**: it holds a generated manifest but does nothing until a human with
admin rank explicitly approves it — mirroring the Steward Memory-Layer
``PROPOSED -> APPROVED`` gate (``fpulse.steward.lessons.LessonStore``).

Two guarantees this store upholds:
  1. **No secrets.** A draft holds only a connector *manifest* — auth
     *templates* like ``"Bearer {token}"``, never a real token. Credentials
     are attached later, on the Connection, via the encrypted credential
     store / Vault. Nothing here ever sees an API key.
  2. **No auto-activation.** ``propose()`` creates a PROPOSED draft; only
     ``approve()`` (called behind an admin-gated API endpoint) turns a
     *runnable* draft into a live Beta connector via
     ``rest_framework.save_user_manifest``.

File-per-draft JSON at ``<FPULSE_DATA_DIR>/connectors/drafts/<id>.json``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_FILE_LOCK = threading.Lock()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_filename(draft_id: str) -> str:
    # Defensive: draft ids are generated, but never trust an id for a path.
    return re.sub(r"[^A-Za-z0-9_.-]", "_", draft_id)[:96]


class DraftStatus(str, Enum):
    """Lifecycle of a drafted connector.

    PROPOSED  — the Copilot generated it; inert, not usable, not in the picker.
    APPROVED  — a human admin accepted it. If the draft is *runnable* it was
                activated as a live Beta connector at approval time.
    REJECTED  — an admin declined it. Kept on disk for audit.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class DraftConnector(BaseModel):
    """A connector the Copilot drafted, awaiting human review."""

    id: str = Field(default_factory=lambda: "conn-draft-" + uuid.uuid4().hex[:10])
    workspace_id: str = "default"

    # Target connector identity
    connector_id: str = Field(description="Intended connector id (letters/digits/underscore).")
    display_name: str = ""
    category: str = "saas"

    # How it was generated + the generated artifact
    mode: str = Field(description="'openapi_runtime' (runnable v1) or 'samples_schema' (v2 draft).")
    runnable: bool = Field(
        default=False,
        description="True when `manifest` is a v1 runtime manifest that can be activated as-is.",
    )
    manifest: dict = Field(default_factory=dict, description="Generated manifest. Auth TEMPLATES only — never a secret.")
    validation: dict = Field(default_factory=dict)
    summary: str = ""
    source: str = Field(default="", description="Provenance, e.g. 'openapi:https://…' or 'samples:3'.")

    # Lifecycle / audit
    status: DraftStatus = DraftStatus.PROPOSED
    proposed_by: str = Field(default="copilot")
    approved_by: str = ""
    reject_reason: str = ""
    activated_connector_id: str = Field(default="", description="Set once approved + activated live.")
    created_at: str = Field(default_factory=_iso_now)
    decided_at: str = ""


class DraftConnectorStore:
    """File-per-draft store at ``<drafts_dir>/<id>.json``. Mirrors LessonStore."""

    def __init__(self, drafts_dir: Path):
        self.dir = Path(drafts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── write side ───────────────────────────────────────────────
    def save(self, draft: DraftConnector) -> DraftConnector:
        path = self.dir / (_safe_filename(draft.id) + ".json")
        tmp = path.with_suffix(".json.tmp")
        with _FILE_LOCK:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(draft.model_dump(mode="json"), fp, indent=2)
            os.replace(tmp, path)
        return draft

    def propose(
        self,
        *,
        connector_id: str,
        mode: str,
        manifest: dict,
        runnable: bool,
        display_name: str = "",
        category: str = "saas",
        validation: dict | None = None,
        summary: str = "",
        source: str = "",
        proposed_by: str = "copilot",
        workspace_id: str = "default",
    ) -> DraftConnector:
        """Create a new PROPOSED draft. Inert until approved."""
        draft = DraftConnector(
            workspace_id=workspace_id or "default",
            connector_id=connector_id,
            display_name=display_name,
            category=category,
            mode=mode,
            runnable=runnable,
            manifest=manifest,
            validation=validation or {},
            summary=summary,
            source=source,
            proposed_by=proposed_by,
        )
        return self.save(draft)

    def approve(self, draft_id: str, approver: str) -> tuple[DraftConnector, dict] | None:
        """Promote PROPOSED -> APPROVED. If the draft is runnable, activate it
        as a live Beta connector FIRST (so a save failure leaves it PROPOSED,
        not falsely APPROVED). Returns (draft, activation_result) or None if the
        draft is missing / not PROPOSED. Raises ValueError if activation of a
        runnable draft fails (bubbled to the API as a 400)."""
        draft = self.get(draft_id)
        if draft is None or draft.status != DraftStatus.PROPOSED:
            return None

        if draft.runnable:
            from fpulse.connectors import rest_framework as rf

            # Force the reviewed id onto the manifest, then persist. May raise
            # ValueError (malformed / id collision) — we let it propagate so the
            # draft stays PROPOSED and the API returns the reason.
            manifest = {**draft.manifest, "id": draft.connector_id}
            saved = rf.save_user_manifest(manifest)
            draft.activated_connector_id = saved.id
            activation = {
                "activated": True,
                "connector_id": saved.id,
                "tier": saved.tier,
                "streams": len(saved.streams),
                "note": "Live as a Beta connector. Create a Connection from it and add the API key there — the key was never part of this draft.",
            }
        else:
            activation = {
                "activated": False,
                "note": (
                    "Schema-only draft (inferred from samples). It captures the response "
                    "shape but not the endpoint or auth, so open it in Insights → Author "
                    "Connector to add the base URL + endpoint + auth, then Save."
                ),
            }

        draft.status = DraftStatus.APPROVED
        draft.approved_by = approver
        draft.decided_at = _iso_now()
        self.save(draft)
        return draft, activation

    def reject(self, draft_id: str, reviewer: str, reason: str = "") -> DraftConnector | None:
        draft = self.get(draft_id)
        if draft is None or draft.status != DraftStatus.PROPOSED:
            return None
        draft.status = DraftStatus.REJECTED
        draft.approved_by = reviewer
        draft.reject_reason = reason
        draft.decided_at = _iso_now()
        return self.save(draft)

    # ── read side ────────────────────────────────────────────────
    def get(self, draft_id: str) -> DraftConnector | None:
        path = self.dir / (_safe_filename(draft_id) + ".json")
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return DraftConnector.model_validate(data)
        except Exception:
            return None

    def list_all(
        self, *, workspace_id: str | None = None, status: DraftStatus | None = None
    ) -> list[DraftConnector]:
        out: list[DraftConnector] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                draft = DraftConnector.model_validate(data)
            except Exception:
                continue
            if workspace_id is not None and draft.workspace_id != workspace_id:
                continue
            if status is not None and draft.status != status:
                continue
            out.append(draft)
        out.sort(key=lambda d: d.created_at, reverse=True)
        return out


def default_drafts_dir() -> Path:
    """`<FPULSE_DATA_DIR or ./data>/connectors/drafts`."""
    data_dir = os.environ.get("FPULSE_DATA_DIR", "").strip() or "data"
    return Path(data_dir).expanduser() / "connectors" / "drafts"


_STORE: DraftConnectorStore | None = None


def default_draft_store() -> DraftConnectorStore:
    """Process-wide store rooted at the configured data dir."""
    global _STORE
    if _STORE is None:
        _STORE = DraftConnectorStore(default_drafts_dir())
    return _STORE
